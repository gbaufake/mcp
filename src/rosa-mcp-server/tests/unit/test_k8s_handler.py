# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").

"""Unit tests for K8s handler (SA token auth + dynamic client)."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from awslabs.rosa_mcp_server.k8s_handler import K8sHandler


@pytest.fixture
def mock_ocm_client():
    mock = MagicMock()
    mock.get_cluster = AsyncMock(return_value={
        'name': 'test-cluster',
        'api': {'url': 'https://api.test-cluster.example.com:443'},
    })
    return mock


@pytest.fixture
def handler(mock_ocm_client):
    mcp = MagicMock()
    mcp.tool = MagicMock(return_value=lambda f: f)
    h = K8sHandler(mcp, mock_ocm_client, allow_write=True, allow_sensitive_data_access=True)
    return h


class TestK8sGetClient:
    """Test SA token auth in k8s_handler."""

    @pytest.mark.asyncio
    async def test_sa_token_found(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('sa-token-123')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        with patch('pathlib.Path.home', return_value=tmp_path):
            client = await handler._get_k8s_client('cluster-id')
            assert client.configuration.host == 'https://api.test.com:443'
            assert 'Bearer sa-token-123' in client.configuration.api_key['authorization']

    @pytest.mark.asyncio
    async def test_no_token_raises(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()

        with patch('pathlib.Path.home', return_value=tmp_path):
            with pytest.raises(ValueError, match='No SA token'):
                await handler._get_k8s_client('cluster-id')

    @pytest.mark.asyncio
    async def test_ocm_down_single_token_fallback(self, handler, tmp_path, mock_ocm_client):
        mock_ocm_client.get_cluster = AsyncMock(side_effect=Exception('OCM down'))
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'fallback.token').write_text('fb-token')
        (token_dir / 'fallback.server').write_text('https://api.fallback.com:443')

        with patch('pathlib.Path.home', return_value=tmp_path):
            client = await handler._get_k8s_client('any-id')
            assert client.configuration.host == 'https://api.fallback.com:443'

    @pytest.mark.asyncio
    async def test_uses_ocm_api_url_when_no_server_file(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('my-token')

        with patch('pathlib.Path.home', return_value=tmp_path):
            client = await handler._get_k8s_client('cluster-id')
            assert client.configuration.host == 'https://api.test-cluster.example.com:443'


class TestListResources:
    """Test rosa_list_resources with various kinds."""

    @pytest.mark.asyncio
    async def test_list_pods(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_pod = MagicMock()
        mock_pod_list = MagicMock()
        mock_pod_list.items = [mock_pod]

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core:
            mock_core.return_value.list_namespaced_pod.return_value = mock_pod_list
            # sanitize_for_serialization returns a dict
            with patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': [{'metadata': {'name': 'test-pod'}}]}):
                result = await handler.rosa_list_resources(None, 'cluster-id', 'Pod', namespace='default')
                data = json.loads(result[0].text)
                assert 'items' in data

    @pytest.mark.asyncio
    async def test_list_hpa_via_dynamic(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_dyn_result = MagicMock()
        mock_dyn_result.to_dict.return_value = {'items': [{'metadata': {'name': 'my-hpa'}}]}

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_resource = MagicMock()
            mock_resource.get.return_value = mock_dyn_result
            mock_dyn_instance.resources.get.return_value = mock_resource
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_list_resources(None, 'cluster-id', 'HorizontalPodAutoscaler')
            data = json.loads(result[0].text)
            assert 'items' in data


class TestManageResource:
    """Test rosa_manage_resource."""

    @pytest.mark.asyncio
    async def test_write_disabled_raises(self, mock_ocm_client):
        mcp = MagicMock()
        mcp.tool = MagicMock(return_value=lambda f: f)
        h = K8sHandler(mcp, mock_ocm_client, allow_write=False, allow_sensitive_data_access=False)

        with pytest.raises(ValueError, match='Write operations disabled'):
            await h.rosa_manage_resource(None, 'cluster-id', 'create', 'Namespace', 'v1', 'test')

    @pytest.mark.asyncio
    async def test_invalid_operation_raises(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        with patch('pathlib.Path.home', return_value=tmp_path):
            with pytest.raises(ValueError, match='Invalid operation'):
                await handler.rosa_manage_resource(None, 'cluster-id', 'invalid', 'Namespace', 'v1', 'test')


class TestGetPodLogs:
    """Test rosa_get_pod_logs."""

    @pytest.mark.asyncio
    async def test_sensitive_data_disabled_raises(self, mock_ocm_client):
        mcp = MagicMock()
        mcp.tool = MagicMock(return_value=lambda f: f)
        h = K8sHandler(mcp, mock_ocm_client, allow_write=False, allow_sensitive_data_access=False)

        with pytest.raises(ValueError, match='Sensitive data access'):
            await h.rosa_get_pod_logs(None, 'cluster-id', 'my-pod')


class TestGetEvents:
    """Test rosa_get_events."""

    @pytest.mark.asyncio
    async def test_sensitive_data_disabled_raises(self, mock_ocm_client):
        mcp = MagicMock()
        mcp.tool = MagicMock(return_value=lambda f: f)
        h = K8sHandler(mcp, mock_ocm_client, allow_write=False, allow_sensitive_data_access=False)

        with pytest.raises(ValueError, match='Sensitive data access'):
            await h.rosa_get_events(None, 'cluster-id')


class TestApplyYaml:
    """Test rosa_apply_yaml."""

    @pytest.mark.asyncio
    async def test_write_disabled_raises(self, mock_ocm_client):
        mcp = MagicMock()
        mcp.tool = MagicMock(return_value=lambda f: f)
        h = K8sHandler(mcp, mock_ocm_client, allow_write=False, allow_sensitive_data_access=False)

        with pytest.raises(ValueError, match='Write operations disabled'):
            await h.rosa_apply_yaml(None, 'cluster-id', 'apiVersion: v1\nkind: Namespace')


class TestToolRegistration:
    """Test all K8s tools are registered."""

    def test_all_tools_registered(self, handler):
        mcp = handler.mcp
        registered = [call.kwargs.get('name', '') for call in mcp.tool.call_args_list]
        expected = [
            'rosa_list_resources', 'rosa_get_pod_logs', 'rosa_get_events',
            'rosa_apply_yaml', 'rosa_get_nodes', 'rosa_manage_resource',
            'rosa_list_api_versions', 'rosa_generate_app_manifest',
        ]
        for name in expected:
            assert name in registered, f'{name} not registered'



class TestGetPodLogs:
    """Test rosa_get_pod_logs with mock."""

    @pytest.mark.asyncio
    async def test_returns_logs(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core:
            mock_core.return_value.read_namespaced_pod_log.return_value = 'line1\nline2\nline3'
            result = await handler.rosa_get_pod_logs(None, 'cid', 'my-pod', namespace='ns')
            assert 'line1' in result[0].text


class TestGetEvents:
    """Test rosa_get_events with mock."""

    @pytest.mark.asyncio
    async def test_returns_events(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_events = MagicMock()
        mock_events.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_namespaced_event.return_value = mock_events
            result = await handler.rosa_get_events(None, 'cid', namespace='default')
            data = json.loads(result[0].text)
            assert 'items' in data


class TestGetNodes:
    """Test rosa_get_nodes with mock."""

    @pytest.mark.asyncio
    async def test_returns_nodes(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_nodes = MagicMock()
        mock_nodes.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_node.return_value = mock_nodes
            result = await handler.rosa_get_nodes(None, 'cid')
            data = json.loads(result[0].text)
            assert 'items' in data


class TestApplyYaml:
    """Test rosa_apply_yaml with mock."""

    @pytest.mark.asyncio
    async def test_applies_yaml(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        yaml_content = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: test\n  namespace: default\ndata:\n  key: value"

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.utils.create_from_dict') as mock_create:
            result = await handler.rosa_apply_yaml(None, 'cid', yaml_content)
            data = json.loads(result[0].text)
            assert 'ConfigMap/test applied' in data['resources']


class TestListApiVersions:
    """Test rosa_list_api_versions."""

    @pytest.mark.asyncio
    async def test_returns_versions(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_core_versions = MagicMock()
        mock_api_groups = MagicMock()

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreApi') as mock_core_api, \
             patch('kubernetes.client.ApisApi') as mock_apis_api, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization') as mock_ser:
            mock_ser.side_effect = [
                {'versions': ['v1']},
                {'groups': [{'name': 'apps', 'versions': [{'groupVersion': 'apps/v1'}], 'preferredVersion': {'groupVersion': 'apps/v1'}}]},
            ]
            mock_core_api.return_value.get_api_versions.return_value = mock_core_versions
            mock_apis_api.return_value.get_api_versions.return_value = mock_api_groups

            result = await handler.rosa_list_api_versions(None, 'cid')
            data = json.loads(result[0].text)
            assert 'v1' in data['core_versions']
            assert data['api_groups'][0]['name'] == 'apps'


class TestGenerateAppManifest:
    """Test rosa_generate_app_manifest."""

    @pytest.mark.asyncio
    async def test_generates_deployment_service_route(self, handler):
        result = await handler.rosa_generate_app_manifest(
            None, app_name='my-app', image_uri='quay.io/test:latest', port=8080
        )
        manifest = result[0].text
        assert 'kind: Deployment' in manifest
        assert 'kind: Service' in manifest
        assert 'kind: Route' in manifest
        assert 'my-app' in manifest
        assert 'quay.io/test:latest' in manifest

    @pytest.mark.asyncio
    async def test_no_route_when_expose_false(self, handler):
        result = await handler.rosa_generate_app_manifest(
            None, app_name='internal', image_uri='img:v1', expose=False
        )
        manifest = result[0].text
        assert 'kind: Deployment' in manifest
        assert 'kind: Route' not in manifest


class TestListResourcesDynamic:
    """Test dynamic fallback for unsupported kinds."""

    @pytest.mark.asyncio
    async def test_list_services(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_svc_list = MagicMock()
        mock_svc_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_namespaced_service.return_value = mock_svc_list
            result = await handler.rosa_list_resources(None, 'cid', 'Service', namespace='default')
            data = json.loads(result[0].text)
            assert 'items' in data

    @pytest.mark.asyncio
    async def test_list_deployments(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_deploy_list = MagicMock()
        mock_deploy_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.AppsV1Api') as mock_apps, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_apps.return_value.list_namespaced_deployment.return_value = mock_deploy_list
            result = await handler.rosa_list_resources(None, 'cid', 'Deployment', namespace='ns')
            data = json.loads(result[0].text)
            assert 'items' in data

    @pytest.mark.asyncio
    async def test_list_configmaps(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_cm_list = MagicMock()
        mock_cm_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_namespaced_config_map.return_value = mock_cm_list
            result = await handler.rosa_list_resources(None, 'cid', 'ConfigMap', namespace='ns')
            data = json.loads(result[0].text)
            assert 'items' in data

    @pytest.mark.asyncio
    async def test_list_namespaces(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_ns_list = MagicMock()
        mock_ns_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_namespace.return_value = mock_ns_list
            result = await handler.rosa_list_resources(None, 'cid', 'Namespace')
            data = json.loads(result[0].text)
            assert 'items' in data



class TestManageResourceOperations:
    """Test manage_resource create/delete/patch/replace."""

    @pytest.mark.asyncio
    async def test_create_resource(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {'kind': 'Namespace', 'metadata': {'name': 'new-ns'}}

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_resource = MagicMock()
            mock_resource.create.return_value = mock_result
            mock_dyn.return_value.resources.get.return_value = mock_resource

            result = await handler.rosa_manage_resource(
                None, 'cid', 'create', 'Namespace', 'v1', 'new-ns',
                body={'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'new-ns'}},
            )
            data = json.loads(result[0].text)
            assert 'create succeeded' in data['message']

    @pytest.mark.asyncio
    async def test_delete_resource(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {'status': 'deleted'}

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_resource = MagicMock()
            mock_resource.delete.return_value = mock_result
            mock_dyn.return_value.resources.get.return_value = mock_resource

            result = await handler.rosa_manage_resource(
                None, 'cid', 'delete', 'Namespace', 'v1', 'old-ns',
            )
            data = json.loads(result[0].text)
            assert 'delete succeeded' in data['message']

    @pytest.mark.asyncio
    async def test_patch_resource(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {'metadata': {'labels': {'env': 'prod'}}}

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_resource = MagicMock()
            mock_resource.patch.return_value = mock_result
            mock_dyn.return_value.resources.get.return_value = mock_resource

            result = await handler.rosa_manage_resource(
                None, 'cid', 'patch', 'Deployment', 'apps/v1', 'my-app',
                namespace='prod', body={'metadata': {'labels': {'env': 'prod'}}},
            )
            data = json.loads(result[0].text)
            assert 'patch succeeded' in data['message']

    @pytest.mark.asyncio
    async def test_replace_resource(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_result = MagicMock()
        mock_result.to_dict.return_value = {'kind': 'ConfigMap'}

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_resource = MagicMock()
            mock_resource.replace.return_value = mock_result
            mock_dyn.return_value.resources.get.return_value = mock_resource

            result = await handler.rosa_manage_resource(
                None, 'cid', 'replace', 'ConfigMap', 'v1', 'my-cm',
                namespace='default', body={'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'my-cm'}},
            )
            data = json.loads(result[0].text)
            assert 'replace succeeded' in data['message']

    @pytest.mark.asyncio
    async def test_body_required_for_create(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        with patch('pathlib.Path.home', return_value=tmp_path):
            with pytest.raises(ValueError, match='body is required'):
                await handler.rosa_manage_resource(None, 'cid', 'create', 'Namespace', 'v1', 'x')


class TestListResourcesAllNamespaces:
    """Test list_resources without namespace (all namespaces)."""

    @pytest.mark.asyncio
    async def test_list_pods_all_ns(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_pod_list = MagicMock()
        mock_pod_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_pod_for_all_namespaces.return_value = mock_pod_list
            result = await handler.rosa_list_resources(None, 'cid', 'Pod')
            data = json.loads(result[0].text)
            assert 'items' in data

    @pytest.mark.asyncio
    async def test_list_events_all_ns(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_list = MagicMock()
        mock_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_event_for_all_namespaces.return_value = mock_list
            result = await handler.rosa_list_resources(None, 'cid', 'Event')
            data = json.loads(result[0].text)
            assert 'items' in data

    @pytest.mark.asyncio
    async def test_list_secrets_ns(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_list = MagicMock()
        mock_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_namespaced_secret.return_value = mock_list
            result = await handler.rosa_list_resources(None, 'cid', 'Secret', namespace='kube-system')
            data = json.loads(result[0].text)
            assert 'items' in data



class TestGetPodLogsExecution:
    """Test pod logs retrieval with container and since_seconds."""

    @pytest.mark.asyncio
    async def test_with_container_and_since(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core:
            mock_core.return_value.read_namespaced_pod_log.return_value = 'container log output'
            result = await handler.rosa_get_pod_logs(
                None, 'cid', 'my-pod', namespace='ns',
                container='sidecar', since_seconds=300, previous=True,
            )
            assert 'container log output' in result[0].text
            call_kwargs = mock_core.return_value.read_namespaced_pod_log.call_args[1]
            assert call_kwargs['container'] == 'sidecar'
            assert call_kwargs['since_seconds'] == 300
            assert call_kwargs['previous'] is True

    @pytest.mark.asyncio
    async def test_empty_logs(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core:
            mock_core.return_value.read_namespaced_pod_log.return_value = ''
            result = await handler.rosa_get_pod_logs(None, 'cid', 'my-pod', namespace='ns')
            assert 'no logs' in result[0].text.lower()


class TestGetEventsFiltered:
    """Test events with resource filters."""

    @pytest.mark.asyncio
    async def test_events_with_resource_filter(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_events = MagicMock()
        mock_events.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_namespaced_event.return_value = mock_events
            result = await handler.rosa_get_events(
                None, 'cid', namespace='prod',
                resource_name='my-pod', resource_kind='Pod',
            )
            call_kwargs = mock_core.return_value.list_namespaced_event.call_args[1]
            assert 'involvedObject.name=my-pod' in call_kwargs['field_selector']
            assert 'involvedObject.kind=Pod' in call_kwargs['field_selector']


class TestListResourcesWithSelectors:
    """Test list_resources with label and field selectors."""

    @pytest.mark.asyncio
    async def test_with_label_selector(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_list = MagicMock()
        mock_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_namespaced_pod.return_value = mock_list
            result = await handler.rosa_list_resources(
                None, 'cid', 'Pod', namespace='ns',
                label_selector='app=nginx', field_selector='status.phase=Running',
            )
            call_kwargs = mock_core.return_value.list_namespaced_pod.call_args[1]
            assert call_kwargs['label_selector'] == 'app=nginx'
            assert call_kwargs['field_selector'] == 'status.phase=Running'



class TestListResourcesMoreKinds:
    """Cover more branches in rosa_list_resources."""

    @pytest.mark.asyncio
    async def test_list_deployments_all_ns(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_list = MagicMock()
        mock_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.AppsV1Api') as mock_apps, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_apps.return_value.list_deployment_for_all_namespaces.return_value = mock_list
            result = await handler.rosa_list_resources(None, 'cid', 'Deployment')
            data = json.loads(result[0].text)
            assert 'items' in data

    @pytest.mark.asyncio
    async def test_list_services_all_ns(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_list = MagicMock()
        mock_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_service_for_all_namespaces.return_value = mock_list
            result = await handler.rosa_list_resources(None, 'cid', 'Service')
            data = json.loads(result[0].text)
            assert 'items' in data

    @pytest.mark.asyncio
    async def test_list_nodes(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_list = MagicMock()
        mock_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_node.return_value = mock_list
            result = await handler.rosa_list_resources(None, 'cid', 'Node')
            data = json.loads(result[0].text)
            assert 'items' in data

    @pytest.mark.asyncio
    async def test_list_secrets_all_ns(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_list = MagicMock()
        mock_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_secret_for_all_namespaces.return_value = mock_list
            result = await handler.rosa_list_resources(None, 'cid', 'Secret')
            data = json.loads(result[0].text)
            assert 'items' in data

    @pytest.mark.asyncio
    async def test_list_configmaps_all_ns(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_list = MagicMock()
        mock_list.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_config_map_for_all_namespaces.return_value = mock_list
            result = await handler.rosa_list_resources(None, 'cid', 'ConfigMap')
            data = json.loads(result[0].text)
            assert 'items' in data

    @pytest.mark.asyncio
    async def test_list_route_dynamic(self, handler, tmp_path, mock_ocm_client):
        """Test dynamic client fallback for Route kind."""
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_dyn_result = MagicMock()
        mock_dyn_result.to_dict.return_value = {'items': []}

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_resource = MagicMock()
            mock_resource.get.return_value = mock_dyn_result
            mock_dyn.return_value.resources.get.return_value = mock_resource

            result = await handler.rosa_list_resources(None, 'cid', 'Route', namespace='prod')
            data = json.loads(result[0].text)
            assert 'items' in data



class TestGetNodesWithSelector:
    @pytest.mark.asyncio
    async def test_with_label_selector(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_nodes = MagicMock()
        mock_nodes.items = []

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core, \
             patch('kubernetes.client.ApiClient.sanitize_for_serialization', return_value={'items': []}):
            mock_core.return_value.list_node.return_value = mock_nodes
            result = await handler.rosa_get_nodes(None, 'cid', label_selector='node-role.kubernetes.io/worker=')
            call_kwargs = mock_core.return_value.list_node.call_args[1]
            assert call_kwargs['label_selector'] == 'node-role.kubernetes.io/worker='


class TestDynamicClientDiscoveryFail:
    @pytest.mark.asyncio
    async def test_unknown_kind_discovery_fails(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.side_effect = Exception('not found')
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_list_resources(None, 'cid', 'CompletelyFakeKind')
            data = json.loads(result[0].text)
            assert 'error' in data or 'Could not discover' in data.get('error', '')
