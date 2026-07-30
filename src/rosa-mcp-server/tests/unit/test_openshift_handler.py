# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").

"""Unit tests for the OpenShift handler (HCP-native tools)."""

import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from awslabs.rosa_mcp_server.rosa_openshift_handler import OpenShiftHandler


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
    h = OpenShiftHandler(mcp, mock_ocm_client, allow_write=False, allow_sensitive_data_access=False)
    return h


class TestParseHelpers:
    """Test CPU and memory parsing helpers."""

    def test_parse_cpu_millicores(self):
        assert OpenShiftHandler._parse_cpu('100m') == 100
        assert OpenShiftHandler._parse_cpu('2500m') == 2500

    def test_parse_cpu_cores(self):
        assert OpenShiftHandler._parse_cpu('1') == 1000
        assert OpenShiftHandler._parse_cpu('2.5') == 2500

    def test_parse_cpu_nanocores(self):
        assert OpenShiftHandler._parse_cpu('1500000n') == 1

    def test_parse_cpu_microcores(self):
        assert OpenShiftHandler._parse_cpu('1500u') == 1

    def test_parse_cpu_zero(self):
        assert OpenShiftHandler._parse_cpu('0') == 0
        assert OpenShiftHandler._parse_cpu('') == 0

    def test_parse_memory_mi(self):
        assert OpenShiftHandler._parse_memory('128Mi') == 128 * 1024 * 1024

    def test_parse_memory_gi(self):
        assert OpenShiftHandler._parse_memory('1Gi') == 1024 * 1024 * 1024

    def test_parse_memory_ki(self):
        assert OpenShiftHandler._parse_memory('1000Ki') == 1000 * 1024

    def test_parse_memory_bytes(self):
        assert OpenShiftHandler._parse_memory('1073741824') == 1073741824

    def test_parse_memory_zero(self):
        assert OpenShiftHandler._parse_memory('0') == 0
        assert OpenShiftHandler._parse_memory('') == 0


class TestGetK8sClient:
    """Test SA token auth logic."""

    @pytest.mark.asyncio
    async def test_sa_token_found(self, handler, tmp_path, mock_ocm_client):
        # Setup token files
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token-123')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        with patch('pathlib.Path.home', return_value=tmp_path):
            client = await handler._get_k8s_client('cluster-id-123')
            assert client.configuration.host == 'https://api.test.com:443'
            assert 'Bearer fake-token-123' in client.configuration.api_key['authorization']

    @pytest.mark.asyncio
    async def test_sa_token_not_found_raises(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        # No token file

        with patch('pathlib.Path.home', return_value=tmp_path):
            with pytest.raises(ValueError, match='No SA token'):
                await handler._get_k8s_client('cluster-id-123')

    @pytest.mark.asyncio
    async def test_ocm_unavailable_single_token_fallback(self, handler, tmp_path, mock_ocm_client):
        mock_ocm_client.get_cluster = AsyncMock(side_effect=Exception('OCM down'))

        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'my-cluster.token').write_text('fallback-token')
        (token_dir / 'my-cluster.server').write_text('https://api.my-cluster.com:443')

        with patch('pathlib.Path.home', return_value=tmp_path):
            client = await handler._get_k8s_client('any-id')
            assert client.configuration.host == 'https://api.my-cluster.com:443'

    @pytest.mark.asyncio
    async def test_ocm_unavailable_multiple_tokens_raises(self, handler, tmp_path, mock_ocm_client):
        mock_ocm_client.get_cluster = AsyncMock(side_effect=Exception('OCM down'))

        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'cluster-a.token').write_text('token-a')
        (token_dir / 'cluster-b.token').write_text('token-b')

        with patch('pathlib.Path.home', return_value=tmp_path):
            with pytest.raises(ValueError, match='multiple SA tokens'):
                await handler._get_k8s_client('any-id')

    @pytest.mark.asyncio
    async def test_server_from_ocm_when_no_server_file(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('my-token')
        # No .server file — should use OCM api url

        with patch('pathlib.Path.home', return_value=tmp_path):
            client = await handler._get_k8s_client('cluster-id-123')
            assert client.configuration.host == 'https://api.test-cluster.example.com:443'


class TestToolRegistration:
    """Test that all tools are registered."""

    def test_all_tools_registered(self, handler):
        mcp = handler.mcp
        registered_names = [
            call.kwargs.get('name', '') for call in mcp.tool.call_args_list
        ]
        expected = [
            'rosa_top_pods', 'rosa_top_nodes', 'rosa_list_routes',
            'rosa_get_quotas', 'rosa_list_projects', 'rosa_rollout_status',
            'rosa_list_hpas', 'rosa_right_size_report',
        ]
        for name in expected:
            assert name in registered_names, f'Tool {name} not registered'



class TestTopPods:
    """Test rosa_top_pods with mocked metrics API."""

    @pytest.mark.asyncio
    async def test_top_pods_returns_sorted_by_cpu(self, handler, tmp_path, mock_ocm_client):
        # Setup token
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_metrics_api = MagicMock()
        mock_item1 = MagicMock()
        mock_item1.metadata.name = 'pod-a'
        mock_item1.metadata.namespace = 'default'
        mock_item1.containers = [{'usage': {'cpu': '500m', 'memory': '128Mi'}}]

        mock_item2 = MagicMock()
        mock_item2.metadata.name = 'pod-b'
        mock_item2.metadata.namespace = 'default'
        mock_item2.containers = [{'usage': {'cpu': '100m', 'memory': '256Mi'}}]

        mock_result = MagicMock()
        mock_result.items = [mock_item1, mock_item2]
        mock_metrics_api.get.return_value = mock_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_metrics_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_top_pods(None, 'cluster-id', namespace='default')
            data = json.loads(result[0].text)
            assert data['pod_count'] == 2
            assert data['pods'][0]['name'] == 'pod-a'  # Higher CPU first
            assert data['pods'][0]['cpu_millicores'] == 500


class TestTopNodes:
    """Test rosa_top_nodes."""

    @pytest.mark.asyncio
    async def test_top_nodes_calculates_utilization(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        # Mock metrics
        mock_metrics_api = MagicMock()
        mock_node_metric = MagicMock()
        mock_node_metric.metadata.name = 'node-1'
        mock_node_metric.__getitem__ = lambda self, key: {'usage': {'cpu': '2000m', 'memory': '4Gi'}}[key] if key == 'usage' else None
        mock_node_metric.get = lambda key, default=None: {'usage': {'cpu': '2000m', 'memory': '4Gi'}}.get(key, default)

        mock_metrics_result = MagicMock()
        mock_metrics_result.items = [mock_node_metric]
        mock_metrics_api.get.return_value = mock_metrics_result

        # Mock node list
        mock_node = MagicMock()
        mock_node.metadata.name = 'node-1'
        mock_node.status.allocatable = {'cpu': '4000m', 'memory': '8Gi'}
        mock_nodes_list = MagicMock()
        mock_nodes_list.items = [mock_node]

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn, \
             patch('kubernetes.client.CoreV1Api') as mock_core:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_metrics_api
            mock_dyn.return_value = mock_dyn_instance
            mock_core.return_value.list_node.return_value = mock_nodes_list

            result = await handler.rosa_top_nodes(None, 'cluster-id')
            data = json.loads(result[0].text)
            assert data['node_count'] == 1
            assert data['nodes'][0]['cpu_percent'] == 50.0
            assert data['nodes'][0]['memory_percent'] == 50.0


class TestListHpas:
    """Test rosa_list_hpas."""

    @pytest.mark.asyncio
    async def test_list_hpas_returns_metrics(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_hpa_api = MagicMock()
        mock_hpa = MagicMock()
        mock_hpa.metadata.name = 'my-hpa'
        mock_hpa.metadata.namespace = 'prod'
        mock_hpa.get = lambda key, default=None: {
            'spec': {
                'scaleTargetRef': {'kind': 'Deployment', 'name': 'my-app'},
                'minReplicas': 2,
                'maxReplicas': 10,
                'metrics': [{'type': 'Resource', 'resource': {'name': 'cpu', 'target': {'type': 'Utilization', 'averageUtilization': 80}}}],
            },
            'status': {
                'currentReplicas': 3,
                'desiredReplicas': 3,
                'currentMetrics': [{'resource': {'current': {'averageUtilization': 45}}}],
                'conditions': [],
            },
        }.get(key, default)

        mock_result = MagicMock()
        mock_result.items = [mock_hpa]
        mock_hpa_api.get.return_value = mock_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_hpa_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_list_hpas(None, 'cluster-id')
            data = json.loads(result[0].text)
            assert data['hpa_count'] == 1
            assert data['hpas'][0]['target_ref'] == 'Deployment/my-app'
            assert data['hpas'][0]['min_replicas'] == 2
            assert data['hpas'][0]['max_replicas'] == 10


class TestAckControllers:
    """Test rosa_list_ack_controllers."""

    @pytest.mark.asyncio
    async def test_lists_controllers_and_crds(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        # Mock deployment
        mock_deploy = MagicMock()
        mock_deploy.metadata.name = 'ack-s3-controller'
        mock_deploy.status.ready_replicas = 1
        mock_deploy.spec.replicas = 1
        mock_deploy.spec.template.spec.containers = [MagicMock(image='public.ecr.aws/ack/s3-controller:v1.0.0')]
        mock_deploy_list = MagicMock()
        mock_deploy_list.items = [mock_deploy]

        # Mock CRD
        mock_crd = MagicMock()
        mock_crd.metadata.name = 'buckets.s3.services.k8s.aws'
        mock_crd.get = lambda key, default=None: {
            'spec': {'names': {'kind': 'Bucket', 'plural': 'buckets'}, 'versions': [{'name': 'v1alpha1'}]},
        }.get(key, default)

        mock_crd_result = MagicMock()
        mock_crd_result.items = [mock_crd]

        mock_crd_api = MagicMock()
        mock_crd_api.get.return_value = mock_crd_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.AppsV1Api') as mock_apps, \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_apps.return_value.list_namespaced_deployment.return_value = mock_deploy_list
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_crd_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_list_ack_controllers(None, 'cluster-id')
            data = json.loads(result[0].text)
            assert data['controller_count'] == 1
            assert data['controllers'][0]['service'] == 's3'
            assert data['controllers'][0]['available'] is True
            assert 's3' in data['ack_services_with_crds']



class TestListRoutes:
    """Test rosa_list_routes."""

    @pytest.mark.asyncio
    async def test_list_routes_returns_route_details(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_route_api = MagicMock()
        mock_route = MagicMock()
        mock_route.metadata.name = 'my-app'
        mock_route.metadata.namespace = 'prod'
        mock_route.get = lambda key, default=None: {
            'spec': {
                'host': 'my-app.apps.example.com',
                'path': '/',
                'tls': {'termination': 'edge'},
                'to': {'name': 'my-app-svc'},
                'port': {'targetPort': 'http'},
            },
            'status': {
                'ingress': [{'conditions': [{'type': 'Admitted', 'status': 'True'}]}],
            },
        }.get(key, default)

        mock_result = MagicMock()
        mock_result.items = [mock_route]
        mock_route_api.get.return_value = mock_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_route_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_list_routes(None, 'cluster-id', namespace='prod')
            data = json.loads(result[0].text)
            assert data['route_count'] == 1
            assert data['routes'][0]['host'] == 'my-app.apps.example.com'
            assert data['routes'][0]['tls_termination'] == 'edge'
            assert data['routes'][0]['admitted'] is True


class TestGetQuotas:
    """Test rosa_get_quotas."""

    @pytest.mark.asyncio
    async def test_get_quotas_returns_quotas_and_limitranges(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        from kubernetes.client import V1ResourceQuota, V1ResourceQuotaStatus, V1ObjectMeta, \
            V1ResourceQuotaList, V1LimitRangeList, V1LimitRange, V1LimitRangeSpec, V1LimitRangeItem

        mock_quota = V1ResourceQuota(
            metadata=V1ObjectMeta(name='compute-quota', namespace='prod'),
            status=V1ResourceQuotaStatus(
                hard={'cpu': '10', 'memory': '32Gi'},
                used={'cpu': '3', 'memory': '8Gi'},
            ),
        )
        mock_lr = V1LimitRange(
            metadata=V1ObjectMeta(name='default-limits', namespace='prod'),
            spec=V1LimitRangeSpec(limits=[
                V1LimitRangeItem(
                    type='Container',
                    default={'cpu': '500m', 'memory': '512Mi'},
                    default_request={'cpu': '100m', 'memory': '128Mi'},
                    max={'cpu': '2', 'memory': '4Gi'},
                    min={'cpu': '50m', 'memory': '64Mi'},
                ),
            ]),
        )

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core:
            mock_core_instance = mock_core.return_value
            mock_core_instance.list_namespaced_resource_quota.return_value = V1ResourceQuotaList(items=[mock_quota])
            mock_core_instance.list_namespaced_limit_range.return_value = V1LimitRangeList(items=[mock_lr])

            result = await handler.rosa_get_quotas(None, 'cluster-id', namespace='prod')
            data = json.loads(result[0].text)
            assert len(data['resource_quotas']) == 1
            assert data['resource_quotas'][0]['hard']['cpu'] == '10'
            assert data['resource_quotas'][0]['used']['cpu'] == '3'
            assert len(data['limit_ranges']) == 1
            assert data['limit_ranges'][0]['limits'][0]['type'] == 'Container'


class TestListProjects:
    """Test rosa_list_projects."""

    @pytest.mark.asyncio
    async def test_list_projects_filters_system(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_project_api = MagicMock()

        def make_project(name, display='', desc='', requester=''):
            p = MagicMock()
            p.metadata.name = name
            p.metadata.get = lambda key, default=None: {
                'annotations': {
                    'openshift.io/display-name': display,
                    'openshift.io/description': desc,
                    'openshift.io/requester': requester,
                },
            }.get(key, default)
            p.get = lambda key, default=None: {'status': {'phase': 'Active'}}.get(key, default)
            return p

        mock_result = MagicMock()
        mock_result.items = [
            make_project('my-app', 'My App', 'Production app', 'admin'),
            make_project('openshift-monitoring'),
            make_project('kube-system'),
            make_project('dev-team', 'Dev Team'),
        ]
        mock_project_api.get.return_value = mock_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_project_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_list_projects(None, 'cluster-id')
            data = json.loads(result[0].text)
            assert data['total_projects'] == 4
            assert data['user_projects'] == 2
            assert data['projects'][0]['name'] == 'my-app'
            assert data['projects'][1]['name'] == 'dev-team'


class TestRolloutStatus:
    """Test rosa_rollout_status."""

    @pytest.mark.asyncio
    async def test_rollout_complete(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_deploy_api = MagicMock()
        mock_deploy = MagicMock()
        mock_deploy.to_dict.return_value = {
            'spec': {'replicas': 3},
            'status': {
                'readyReplicas': 3,
                'availableReplicas': 3,
                'updatedReplicas': 3,
                'observedGeneration': 5,
                'conditions': [
                    {'type': 'Available', 'status': 'True', 'reason': 'MinimumReplicasAvailable', 'message': 'ok', 'lastUpdateTime': '2026-01-01'},
                ],
            },
        }
        mock_deploy_api.get.return_value = mock_deploy

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_deploy_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_rollout_status(None, 'cluster-id', name='my-app', namespace='prod')
            data = json.loads(result[0].text)
            assert data['rollout_status'] == 'Complete'
            assert data['replicas_ready'] == 3
            assert data['replicas_desired'] == 3



class TestRightSizeReport:
    """Test rosa_right_size_report."""

    @pytest.mark.asyncio
    async def test_identifies_over_provisioned(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        from kubernetes.client import V1Deployment, V1ObjectMeta, V1DeploymentSpec, \
            V1LabelSelector, V1PodTemplateSpec, V1PodSpec, V1Container, \
            V1ResourceRequirements, V1DeploymentStatus, V1DeploymentList

        deploy = V1Deployment(
            metadata=V1ObjectMeta(name='my-app', namespace='prod'),
            spec=V1DeploymentSpec(
                replicas=2,
                selector=V1LabelSelector(match_labels={'app': 'my-app'}),
                template=V1PodTemplateSpec(
                    metadata=V1ObjectMeta(labels={'app': 'my-app'}),
                    spec=V1PodSpec(containers=[
                        V1Container(
                            name='app',
                            resources=V1ResourceRequirements(
                                requests={'cpu': '1000m', 'memory': '1Gi'},
                                limits={'cpu': '2000m', 'memory': '2Gi'},
                            ),
                        ),
                    ]),
                ),
            ),
            status=V1DeploymentStatus(ready_replicas=2),
        )

        # Mock metrics - low usage
        mock_metrics_api = MagicMock()
        mock_pod = MagicMock()
        mock_pod.metadata.name = 'my-app-abc123'
        mock_pod.metadata.namespace = 'prod'
        mock_pod.containers = [{'usage': {'cpu': '50m', 'memory': '128Mi'}}]
        mock_metrics_result = MagicMock()
        mock_metrics_result.items = [mock_pod]
        mock_metrics_api.get.return_value = mock_metrics_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.AppsV1Api') as mock_apps, \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_apps.return_value.list_namespaced_deployment.return_value = V1DeploymentList(items=[deploy])
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_metrics_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_right_size_report(None, 'cluster-id', namespace='prod')
            data = json.loads(result[0].text)
            assert data['summary']['over_provisioned'] == 1
            assert data['deployments'][0]['status'] == 'over-provisioned'
            assert data['deployments'][0]['recommendation']['cpu_request'] != 'keep'


class TestAckResources:
    """Test rosa_list_ack_resources."""

    @pytest.mark.asyncio
    async def test_no_crds_returns_error(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_crd_api = MagicMock()
        mock_crd_result = MagicMock()
        mock_crd_result.items = []  # No CRDs
        mock_crd_api.get.return_value = mock_crd_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_crd_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_list_ack_resources(None, 'cluster-id', service='nonexistent')
            data = json.loads(result[0].text)
            assert 'error' in data

    @pytest.mark.asyncio
    async def test_lists_resources_for_service(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        # CRD mock
        mock_crd = MagicMock()
        mock_crd.metadata.name = 'buckets.s3.services.k8s.aws'
        mock_crd.get = lambda key, default=None: {
            'spec': {'names': {'kind': 'Bucket', 'plural': 'buckets'}, 'versions': [{'name': 'v1alpha1'}]},
        }.get(key, default)

        mock_crd_result = MagicMock()
        mock_crd_result.items = [mock_crd]

        # Resource mock
        mock_bucket = MagicMock()
        mock_bucket.metadata.name = 'my-bucket'
        mock_bucket.metadata.namespace = 'default'
        mock_bucket.get = lambda key, default=None: {
            'status': {
                'conditions': [{'type': 'ACK.ResourceSynced', 'status': 'True', 'message': ''}],
                'ackResourceMetadata': {'arn': 'arn:aws:s3:::my-bucket'},
            },
        }.get(key, default)

        mock_resource_result = MagicMock()
        mock_resource_result.items = [mock_bucket]

        mock_crd_api = MagicMock()
        mock_crd_api.get.return_value = mock_crd_result

        mock_resource_api = MagicMock()
        mock_resource_api.get.return_value = mock_resource_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            # First call = CRD api, second call = resource api
            mock_dyn_instance.resources.get.side_effect = [mock_crd_api, mock_resource_api]
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_list_ack_resources(None, 'cluster-id', service='s3')
            data = json.loads(result[0].text)
            assert data['service'] == 's3'
            assert data['resource_count'] == 1
            assert data['resources'][0]['name'] == 'my-bucket'
            assert data['resources'][0]['synced'] is True



class TestTopPodsAllNamespaces:
    @pytest.mark.asyncio
    async def test_top_pods_no_namespace(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_metrics_api = MagicMock()
        mock_result = MagicMock()
        mock_result.items = []
        mock_metrics_api.get.return_value = mock_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_metrics_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_top_pods(None, 'cluster-id')
            data = json.loads(result[0].text)
            assert data['pod_count'] == 0

    @pytest.mark.asyncio
    async def test_top_pods_sort_by_memory(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_metrics_api = MagicMock()
        mock_item = MagicMock()
        mock_item.metadata.name = 'pod-mem'
        mock_item.metadata.namespace = 'ns'
        mock_item.containers = [{'usage': {'cpu': '10m', 'memory': '1Gi'}}]
        mock_result = MagicMock()
        mock_result.items = [mock_item]
        mock_metrics_api.get.return_value = mock_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_metrics_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_top_pods(None, 'cluster-id', sort_by='memory')
            data = json.loads(result[0].text)
            assert data['pods'][0]['memory_mib'] > 900


class TestListRoutesAllNs:
    @pytest.mark.asyncio
    async def test_routes_with_label_selector(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('fake-token')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_route_api = MagicMock()
        mock_result = MagicMock()
        mock_result.items = []
        mock_route_api.get.return_value = mock_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_route_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_list_routes(None, 'cluster-id', label_selector='app=web')
            data = json.loads(result[0].text)
            assert data['route_count'] == 0
            mock_route_api.get.assert_called_once_with(label_selector='app=web')



class TestGetQuotasAllNs:
    @pytest.mark.asyncio
    async def test_quotas_all_namespaces(self, handler, tmp_path, mock_ocm_client):
        from kubernetes.client import V1ResourceQuotaList, V1LimitRangeList
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('t')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.client.CoreV1Api') as mock_core:
            mock_core.return_value.list_resource_quota_for_all_namespaces.return_value = V1ResourceQuotaList(items=[])
            mock_core.return_value.list_limit_range_for_all_namespaces.return_value = V1LimitRangeList(items=[])
            result = await handler.rosa_get_quotas(None, 'cid')
            data = json.loads(result[0].text)
            assert data['resource_quotas'] == []
            assert data['limit_ranges'] == []


class TestRolloutStatusDC:
    @pytest.mark.asyncio
    async def test_deploymentconfig_kind(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('t')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_dc_api = MagicMock()
        mock_dc = MagicMock()
        mock_dc.to_dict.return_value = {
            'spec': {'replicas': 1},
            'status': {'readyReplicas': 1, 'availableReplicas': 1, 'updatedReplicas': 1, 'observedGeneration': 2, 'conditions': []},
        }
        mock_dc_api.get.return_value = mock_dc

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn_instance = MagicMock()
            mock_dyn_instance.resources.get.return_value = mock_dc_api
            mock_dyn.return_value = mock_dyn_instance

            result = await handler.rosa_rollout_status(None, 'cid', name='legacy-app', namespace='prod', kind='DeploymentConfig')
            data = json.loads(result[0].text)
            assert data['kind'] == 'DeploymentConfig'
            assert data['rollout_status'] == 'Complete'



class TestRolloutStatusProgressing:
    @pytest.mark.asyncio
    async def test_rollout_progressing(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('t')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_api = MagicMock()
        mock_deploy = MagicMock()
        mock_deploy.to_dict.return_value = {
            'spec': {'replicas': 3},
            'status': {'readyReplicas': 1, 'availableReplicas': 1, 'updatedReplicas': 2, 'observedGeneration': 5, 'conditions': []},
        }
        mock_api.get.return_value = mock_deploy

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn.return_value.resources.get.return_value = mock_api

            result = await handler.rosa_rollout_status(None, 'cid', name='rolling', namespace='ns')
            data = json.loads(result[0].text)
            assert data['rollout_status'] == 'Progressing'


class TestListHpasAllNs:
    @pytest.mark.asyncio
    async def test_hpas_all_namespaces(self, handler, tmp_path, mock_ocm_client):
        token_dir = tmp_path / '.rosa-mcp'
        token_dir.mkdir()
        (token_dir / 'test-cluster.token').write_text('t')
        (token_dir / 'test-cluster.server').write_text('https://api.test.com:443')

        mock_hpa_api = MagicMock()
        mock_result = MagicMock()
        mock_result.items = []
        mock_hpa_api.get.return_value = mock_result

        with patch('pathlib.Path.home', return_value=tmp_path), \
             patch('kubernetes.dynamic.DynamicClient') as mock_dyn:
            mock_dyn.return_value.resources.get.return_value = mock_hpa_api

            result = await handler.rosa_list_hpas(None, 'cid')
            data = json.loads(result[0].text)
            assert data['hpa_count'] == 0
            mock_hpa_api.get.assert_called_once_with()
