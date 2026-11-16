import logging
import os
import platform
import pytest
import tempfile
import yaml
from mcp_server_opensearch.clusters_information import (
    ClusterInfo,
    _resolve_env_references,
    add_cluster,
    cluster_registry,
    get_cluster,
    load_clusters_from_yaml,
)
from opensearch.client import ConfigurationError


class TestEnvironmentReferences:
    @pytest.mark.parametrize(
        ('field', 'error_type'), [('timeout', 'int_parsing'), ('ssl_verify', 'bool_parsing')]
    )
    async def test_validation_errors_hide_expanded_secret(
        self, tmp_path, monkeypatch, caplog, field, error_type
    ):
        secret = 'sensitive-password'
        monkeypatch.setenv('SECRET_VALUE', secret)
        monkeypatch.delenv('OPENSEARCH_HEADER_AUTH', raising=False)
        path = tmp_path / 'clusters.yml'
        path.write_text(
            yaml.safe_dump(
                {
                    'clusters': {
                        'invalid': {
                            'opensearch_url': 'https://example.com',
                            field: '${SECRET_VALUE}',
                        }
                    }
                }
            )
        )
        cluster_registry.clear()
        await load_clusters_from_yaml(str(path))
        expected_errors = [
            f"Error processing cluster 'invalid': {[{'loc': (field,), 'type': error_type}]}"
        ]
        assert caplog.messages == [f'Loading errors: {expected_errors}']
        assert secret not in caplog.text
        assert cluster_registry == {}

    async def test_other_cluster_errors_hide_values(self, tmp_path, monkeypatch, caplog):
        secret = 'sensitive-password'
        monkeypatch.delenv('OPENSEARCH_HEADER_AUTH', raising=False)
        path = tmp_path / 'clusters.yml'
        path.write_text('clusters:\n  invalid:\n    opensearch_url: https://example.com\n')

        def fail_registration(**kwargs):
            raise ValueError(secret)

        monkeypatch.setattr(
            'mcp_server_opensearch.clusters_information.add_cluster', fail_registration
        )
        cluster_registry.clear()
        await load_clusters_from_yaml(str(path))
        assert caplog.messages == [
            'Loading errors: ["Error processing cluster \'invalid\': ValueError"]'
        ]
        assert secret not in caplog.text
        assert cluster_registry == {}

    @pytest.mark.parametrize('enabled', ['true', ' TrUe '])
    async def test_header_auth_skips_unused_yaml_clusters(
        self, tmp_path, monkeypatch, caplog, enabled
    ):
        monkeypatch.setenv('OPENSEARCH_HEADER_AUTH', enabled)
        monkeypatch.delenv('MISSING_VALUE', raising=False)
        path = tmp_path / 'clusters.yml'
        path.write_text(
            yaml.safe_dump(
                {
                    'clusters': {
                        'literal': {'opensearch_url': 'https://example.com'},
                        'unused': {'opensearch_url': '${MISSING_VALUE}'},
                    }
                }
            )
        )
        cluster_registry.clear()
        with caplog.at_level(logging.INFO):
            await load_clusters_from_yaml(str(path))
        assert cluster_registry == {}
        assert caplog.messages == [
            'Skipping YAML clusters because header authentication is enabled'
        ]

    def test_escape_preserves_two_dollars(self):
        assert _resolve_env_references('$$${HOME}', 'example') == '$${HOME}'

    @pytest.mark.parametrize(
        'value', ['  password with spaces  ', '$PLAIN', '${not-valid}', 'line\nnext', '£€']
    )
    def test_literal_unchanged(self, value):
        assert _resolve_env_references(value, 'example') == value

    @pytest.mark.parametrize('value', [None, True, False, 42, 1.5])
    def test_non_string_unchanged(self, value):
        assert _resolve_env_references(value, 'example') is value

    def test_nested_values_and_literal_keys(self, monkeypatch):
        monkeypatch.setenv('CLUSTER_VALUE', '  resolved  ')
        value = {'${CLUSTER_VALUE}': [{'password': '${CLUSTER_VALUE}'}, 42, None, True]}
        assert _resolve_env_references(value, 'example') == {
            '${CLUSTER_VALUE}': [{'password': '  resolved  '}, 42, None, True]
        }
        assert value['${CLUSTER_VALUE}'][0]['password'] == '${CLUSTER_VALUE}'

    def test_nested_missing_field(self, monkeypatch):
        monkeypatch.delenv('MISSING_VALUE', raising=False)
        with pytest.raises(
            ConfigurationError, match=r'example.*options\[0\].password.*MISSING_VALUE'
        ):
            _resolve_env_references({'options': [{'password': '${MISSING_VALUE}'}]}, 'example')

    @pytest.mark.parametrize('env_value', [None, '', 'configured'])
    @pytest.mark.parametrize('default', ['', '  fallback  '])
    def test_default(self, monkeypatch, env_value, default):
        if env_value is None:
            monkeypatch.delenv('CONFIG_VALUE', raising=False)
        else:
            monkeypatch.setenv('CONFIG_VALUE', env_value)
        assert _resolve_env_references('${CONFIG_VALUE:-' + default + '}', 'example') == (
            env_value or default
        )

    def test_empty_variable_without_default(self, monkeypatch):
        monkeypatch.setenv('CONFIG_VALUE', '')
        assert _resolve_env_references('${CONFIG_VALUE}', 'example') == ''

    @pytest.mark.parametrize('env_value', [None, 'configured'])
    def test_escape(self, monkeypatch, env_value):
        if env_value is None:
            monkeypatch.delenv('CONFIG_VALUE', raising=False)
        else:
            monkeypatch.setenv('CONFIG_VALUE', env_value)
        assert _resolve_env_references(
            '$${CONFIG_VALUE}/$${CONFIG_VALUE:-default}', 'example'
        ) == ('${CONFIG_VALUE}/${CONFIG_VALUE:-default}')

    async def test_load_references_in_all_fields(self, tmp_path, monkeypatch):
        values = {
            'opensearch_url': 'https://example.com:9200',
            'opensearch_username': 'user',
            'opensearch_password': '  password: true\nclusters: [injected] ${UNCHANGED}  ',
            'iam_arn': 'arn:aws:iam::123456789012:role/ExampleRole',
            'profile': 'example',
            'aws_region': 'us-east-1',
            'opensearch_ca_cert_path': '/certs/ca.crt',
            'opensearch_client_cert_path': '/certs/tls.crt',
            'opensearch_client_key_path': '/certs/tls.key',
        }
        config = {}
        for field, value in values.items():
            variable = field.upper()
            monkeypatch.setenv(variable, value)
            config[field] = '${' + variable + '}'
        config.update(
            timeout='${TIMEOUT:-45}', ssl_verify='${VERIFY:-false}', opensearch_no_auth=False
        )
        monkeypatch.delenv('TIMEOUT', raising=False)
        monkeypatch.delenv('VERIFY', raising=False)
        path = tmp_path / 'clusters.yml'
        path.write_text(yaml.safe_dump({'clusters': {'env-cluster': config}}))
        cluster_registry.clear()
        try:
            await load_clusters_from_yaml(str(path))
            cluster = get_cluster('env-cluster')
            for field, value in values.items():
                assert getattr(cluster, field) == value
            assert cluster.timeout == 45
            assert cluster.ssl_verify is False
            assert cluster.opensearch_no_auth is False
            assert set(cluster_registry) == {'env-cluster'}
        finally:
            cluster_registry.clear()

    async def test_load_literal_password_unchanged(self, tmp_path):
        password = '  literal $VALUE: true\nnext line  '
        path = tmp_path / 'clusters.yml'
        path.write_text(
            yaml.safe_dump(
                {
                    'clusters': {
                        'literal': {
                            'opensearch_url': 'https://example.com',
                            'opensearch_password': password,
                        }
                    }
                }
            )
        )
        cluster_registry.clear()
        try:
            await load_clusters_from_yaml(str(path))
            assert get_cluster('literal').opensearch_password == password
        finally:
            cluster_registry.clear()

    async def test_unset_error_without_secret_or_partial_registry(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setenv('SECRET_VALUE', 'sensitive-password')
        monkeypatch.delenv('MISSING_VALUE', raising=False)
        path = tmp_path / 'clusters.yml'
        path.write_text(
            yaml.safe_dump(
                {
                    'clusters': {
                        'first': {'opensearch_url': 'https://example.com'},
                        'second': {
                            'opensearch_url': 'https://example.com',
                            'opensearch_password': '${SECRET_VALUE}:${MISSING_VALUE}',
                        },
                    }
                }
            )
        )
        cluster_registry.clear()
        with pytest.raises(ConfigurationError) as error:
            await load_clusters_from_yaml(str(path))
        message = str(error.value)
        assert message == (
            "Cluster 'second', field 'opensearch_password': "
            "environment variable 'MISSING_VALUE' is not set"
        )
        assert 'sensitive-password' not in message + caplog.text
        assert cluster_registry == {}

    def test_embedded_multiple_references(self, monkeypatch):
        monkeypatch.setenv('HOST', 'example.com')
        monkeypatch.setenv('PORT', '9200')
        assert _resolve_env_references('https://${HOST}:${PORT}', 'example') == (
            'https://example.com:9200'
        )


class TestClusterInfo:
    def test_cluster_info_creation_with_required_fields(self):
        """Test creating ClusterInfo with only required fields."""
        cluster = ClusterInfo(opensearch_url='https://localhost:9200')
        assert cluster.opensearch_url == 'https://localhost:9200'
        assert cluster.iam_arn is None
        assert cluster.aws_region is None
        assert cluster.opensearch_username is None
        assert cluster.opensearch_password is None
        assert cluster.profile is None

    def test_cluster_info_creation_with_all_fields(self):
        """Test creating ClusterInfo with all fields."""
        cluster = ClusterInfo(
            opensearch_url='https://localhost:9200',
            iam_arn='arn:aws:iam::123456789012:role/OpenSearchRole',
            aws_region='us-west-2',
            opensearch_username='admin',
            opensearch_password='password123',
            profile='default',
            timeout=30,
            opensearch_no_auth=True,
            opensearch_ca_cert_path='/tmp/ca.pem',
            opensearch_client_cert_path='/tmp/client.pem',
            opensearch_client_key_path='/tmp/client.key',
        )
        assert cluster.opensearch_url == 'https://localhost:9200'
        assert cluster.iam_arn == 'arn:aws:iam::123456789012:role/OpenSearchRole'
        assert cluster.aws_region == 'us-west-2'
        assert cluster.opensearch_username == 'admin'
        assert cluster.opensearch_password == 'password123'
        assert cluster.profile == 'default'
        assert cluster.timeout == 30
        assert cluster.opensearch_no_auth is True
        assert cluster.opensearch_ca_cert_path == '/tmp/ca.pem'
        assert cluster.opensearch_client_cert_path == '/tmp/client.pem'
        assert cluster.opensearch_client_key_path == '/tmp/client.key'

    def test_cluster_info_with_timeout_only(self):
        """Test creating ClusterInfo with timeout parameter."""
        cluster = ClusterInfo(opensearch_url='https://localhost:9200', timeout=60)
        assert cluster.timeout == 60

    def test_cluster_info_with_no_auth_only(self):
        """Test creating ClusterInfo with opensearch_no_auth parameter."""
        cluster = ClusterInfo(opensearch_url='https://localhost:9200', opensearch_no_auth=True)
        assert cluster.opensearch_no_auth is True
        assert cluster.opensearch_url == 'https://localhost:9200'

    def test_cluster_info_no_auth_defaults_to_none(self):
        """Test that opensearch_no_auth defaults to None when not specified."""
        cluster = ClusterInfo(opensearch_url='https://localhost:9200')
        assert cluster.opensearch_no_auth is None

    def test_cluster_info_validation(self):
        """Test that ClusterInfo validates required fields."""
        with pytest.raises(ValueError):
            ClusterInfo()  # Missing required opensearch_url


class TestClusterRegistry:
    def setup_method(self):
        """Clear the cluster registry before each test."""
        cluster_registry.clear()

    def test_add_cluster(self):
        """Test adding a cluster to the registry."""
        cluster = ClusterInfo(opensearch_url='https://localhost:9200')
        add_cluster('test-cluster', cluster)

        assert 'test-cluster' in cluster_registry
        assert cluster_registry['test-cluster'] == cluster

    def test_add_cluster_overwrites_existing(self):
        """Test that adding a cluster with existing name overwrites it."""
        cluster1 = ClusterInfo(opensearch_url='https://localhost:9200')
        cluster2 = ClusterInfo(opensearch_url='https://localhost:9201')

        add_cluster('test-cluster', cluster1)
        add_cluster('test-cluster', cluster2)

        assert cluster_registry['test-cluster'] == cluster2
        assert len(cluster_registry) == 1

    def test_get_cluster_existing(self):
        """Test getting an existing cluster."""
        cluster = ClusterInfo(opensearch_url='https://localhost:9200')
        add_cluster('test-cluster', cluster)

        retrieved = get_cluster('test-cluster')
        assert retrieved == cluster

    def test_get_cluster_nonexistent(self):
        """Test getting a non-existent cluster returns None."""
        retrieved = get_cluster('nonexistent-cluster')
        assert retrieved is None


class TestLoadClustersFromYaml:
    def setup_method(self):
        """Clear the cluster registry before each test."""
        cluster_registry.clear()

    @pytest.mark.asyncio
    async def test_load_clusters_from_yaml_empty_file(self):
        """Test loading from empty YAML file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            f.write('clusters: {}')
            f.flush()

            await load_clusters_from_yaml(f.name)

        os.unlink(f.name)
        assert len(cluster_registry) == 0

    @pytest.mark.asyncio
    async def test_load_clusters_from_yaml_valid_clusters(self):
        """Test loading valid clusters from YAML."""
        yaml_content = """
clusters:
  cluster1:
    opensearch_url: "https://localhost:9200"
    opensearch_username: "admin"
    opensearch_password: "password"
    timeout: 45
    opensearch_ca_cert_path: "/etc/opensearch/ca.pem"
    opensearch_client_cert_path: "/etc/opensearch/tls.crt"
    opensearch_client_key_path: "/etc/opensearch/tls.key"
  cluster2:
    opensearch_url: "https://localhost:9201"
    iam_arn: "arn:aws:iam::123456789012:role/OpenSearchRole"
    aws_region: "us-west-2"
"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            await load_clusters_from_yaml(f.name)

        os.unlink(f.name)

        assert len(cluster_registry) == 2
        assert 'cluster1' in cluster_registry
        assert 'cluster2' in cluster_registry

        cluster1 = cluster_registry['cluster1']
        assert cluster1.opensearch_url == 'https://localhost:9200'
        assert cluster1.opensearch_username == 'admin'
        assert cluster1.opensearch_password == 'password'
        assert cluster1.timeout == 45
        assert cluster1.opensearch_ca_cert_path == '/etc/opensearch/ca.pem'
        assert cluster1.opensearch_client_cert_path == '/etc/opensearch/tls.crt'
        assert cluster1.opensearch_client_key_path == '/etc/opensearch/tls.key'

        cluster2 = cluster_registry['cluster2']
        assert cluster2.opensearch_url == 'https://localhost:9201'
        assert cluster2.iam_arn == 'arn:aws:iam::123456789012:role/OpenSearchRole'
        assert cluster2.aws_region == 'us-west-2'
        assert cluster2.timeout is None

    @pytest.mark.asyncio
    async def test_load_clusters_from_yaml_with_no_auth(self):
        """Test loading cluster with opensearch_no_auth from YAML."""
        yaml_content = """
clusters:
  no-auth-cluster:
    opensearch_url: "http://localhost:9200"
    opensearch_no_auth: true
  mixed-cluster:
    opensearch_url: "https://localhost:9201"
    opensearch_username: "admin"
    opensearch_password: "password"
    opensearch_no_auth: false
"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            await load_clusters_from_yaml(f.name)

        os.unlink(f.name)

        assert len(cluster_registry) == 2

        # Test no-auth cluster
        no_auth_cluster = cluster_registry['no-auth-cluster']
        assert no_auth_cluster.opensearch_url == 'http://localhost:9200'
        assert no_auth_cluster.opensearch_no_auth is True

        # Test mixed cluster with explicit false
        mixed_cluster = cluster_registry['mixed-cluster']
        assert mixed_cluster.opensearch_url == 'https://localhost:9201'
        assert mixed_cluster.opensearch_username == 'admin'
        assert mixed_cluster.opensearch_password == 'password'
        assert mixed_cluster.opensearch_no_auth is False

    @pytest.mark.asyncio
    async def test_load_clusters_from_yaml_missing_opensearch_url(self):
        """Test loading cluster without required opensearch_url."""
        yaml_content = """
clusters:
  invalid_cluster:
    opensearch_username: "admin"
    opensearch_password: "password"
"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            await load_clusters_from_yaml(f.name)

        os.unlink(f.name)

        # Cluster should not be added due to missing opensearch_url
        assert len(cluster_registry) == 0

    @pytest.mark.asyncio
    async def test_load_clusters_from_yaml_without_connection_check(self):
        """Test loading cluster without connection validation."""
        yaml_content = """
clusters:
  unreachable_cluster:
    opensearch_url: "https://unreachable:9200"
"""

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            f.write(yaml_content)
            f.flush()
            await load_clusters_from_yaml(f.name)

        os.unlink(f.name)

        # Cluster should be added even if potentially unreachable (no connection check)
        assert len(cluster_registry) == 1
        assert 'unreachable_cluster' in cluster_registry
        assert cluster_registry['unreachable_cluster'].opensearch_url == 'https://unreachable:9200'

    @pytest.mark.asyncio
    async def test_load_clusters_from_yaml_file_not_found(self):
        """Test loading from non-existent file."""
        with pytest.raises(FileNotFoundError):
            await load_clusters_from_yaml('/nonexistent/file.yml')

    @pytest.mark.asyncio
    async def test_load_clusters_from_yaml_invalid_yaml(self):
        """Test loading from invalid YAML file."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            f.write('invalid: yaml: content: [')
            f.flush()

            with pytest.raises(yaml.YAMLError):
                await load_clusters_from_yaml(f.name)

        os.unlink(f.name)

    @pytest.mark.asyncio
    async def test_load_clusters_from_yaml_permission_error(self):
        """Test loading from file with permission error."""
        # Skip this test on Windows as os.chmod with 0o000 doesn't work reliably
        if platform.system() == 'Windows':
            pytest.skip('Permission test not reliable on Windows')

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            f.write('clusters: {}')
            f.flush()
            os.chmod(f.name, 0o000)  # Remove all permissions

            with pytest.raises(PermissionError):
                await load_clusters_from_yaml(f.name)

        os.chmod(f.name, 0o644)  # Restore permissions
        os.unlink(f.name)

    @pytest.mark.asyncio
    async def test_load_clusters_from_yaml_empty_path(self):
        """Test loading from empty path."""
        await load_clusters_from_yaml('')
        assert len(cluster_registry) == 0

    @pytest.mark.asyncio
    async def test_load_clusters_from_yaml_none_path(self):
        """Test loading from None path."""
        await load_clusters_from_yaml(None)
        assert len(cluster_registry) == 0
