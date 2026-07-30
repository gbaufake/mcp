# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""OpenShift-native operations handler.

Provides tools for OpenShift-specific resources and operations that go beyond
standard Kubernetes: top pods/nodes (metrics-server), Routes, Projects,
ResourceQuotas, LimitRanges, ClusterOperators, DeploymentConfigs, and rollouts.
"""

import json
from awslabs.rosa_mcp_server.ocm_client import OCMClient
from kubernetes import client as k8s_client
from kubernetes import dynamic
from mcp.server.fastmcp import Context
from mcp.types import TextContent
from typing import Optional



class OpenShiftHandler:
    """Handler for OpenShift-native operations beyond standard Kubernetes."""

    def __init__(
        self,
        mcp,
        ocm_client: OCMClient,
        allow_write: bool = False,
        allow_sensitive_data_access: bool = False,
    ):
        """Initialize the handler.

        Args:
            mcp: The FastMCP server instance.
            ocm_client: Shared OCM API client for fetching cluster credentials.
            allow_write: Whether write operations are permitted.
            allow_sensitive_data_access: Whether sensitive data access is permitted.
        """
        self.mcp = mcp
        self.ocm = ocm_client
        self.allow_write = allow_write
        self.allow_sensitive_data_access = allow_sensitive_data_access

        # Register tools
        self.mcp.tool(name='rosa_top_pods')(self.rosa_top_pods)
        self.mcp.tool(name='rosa_top_nodes')(self.rosa_top_nodes)
        self.mcp.tool(name='rosa_list_routes')(self.rosa_list_routes)
        self.mcp.tool(name='rosa_get_quotas')(self.rosa_get_quotas)
        self.mcp.tool(name='rosa_list_projects')(self.rosa_list_projects)
        self.mcp.tool(name='rosa_rollout_status')(self.rosa_rollout_status)
        self.mcp.tool(name='rosa_list_hpas')(self.rosa_list_hpas)
        self.mcp.tool(name='rosa_right_size_report')(self.rosa_right_size_report)
        self.mcp.tool(name='rosa_list_ack_controllers')(self.rosa_list_ack_controllers)
        self.mcp.tool(name='rosa_list_ack_resources')(self.rosa_list_ack_resources)


    async def _get_k8s_client(self, cluster_id: str) -> k8s_client.ApiClient:
        """Get a configured kubernetes API client for the given cluster.

        Auth: SA token file from ~/.rosa-mcp/<cluster-name>.token (HCP-native).
        The SA must be pre-created in the cluster with appropriate RBAC.

        Tries to resolve cluster name from OCM. If OCM is unavailable (token expired),
        falls back to scanning ~/.rosa-mcp/*.token files for a match.
        """
        import pathlib

        token_dir = pathlib.Path.home() / '.rosa-mcp'
        cluster_name = None
        api_url = None

        # Try resolving cluster name from OCM
        try:
            cluster_info = await self.ocm.get_cluster(cluster_id)
            cluster_name = cluster_info.get('name', '')
            api_url = cluster_info.get('api', {}).get('url', '')
        except Exception:
            # OCM unavailable — scan token dir for any token file
            # and try to match by listing available tokens
            pass

        if cluster_name:
            token_file = token_dir / f'{cluster_name}.token'
            server_file = token_dir / f'{cluster_name}.server'
        else:
            # Fallback: try all token files in ~/.rosa-mcp/
            token_files = list(token_dir.glob('*.token')) if token_dir.exists() else []
            if len(token_files) == 1:
                token_file = token_files[0]
                cluster_name = token_file.stem
                server_file = token_dir / f'{cluster_name}.server'
            elif token_files:
                # Multiple tokens — can't determine which one without OCM
                names = [f.stem for f in token_files]
                raise ValueError(
                    f'OCM unavailable and multiple SA tokens found: {names}. '
                    'Please refresh OCM token (ocm login) or remove unused tokens.'
                )
            else:
                raise ValueError(
                    f'No SA token found in {token_dir}. '
                    'Setup: oc create ns rosa-mcp-system && '
                    'oc create sa rosa-mcp-sa -n rosa-mcp-system && '
                    'oc adm policy add-cluster-role-to-user cluster-admin '
                    'system:serviceaccount:rosa-mcp-system:rosa-mcp-sa && '
                    'then save the token to ~/.rosa-mcp/<cluster-name>.token'
                )

        if not token_file.exists():
            raise ValueError(
                f'No SA token found for cluster "{cluster_name}" at {token_file}. '
                'Save the SA token to that path.'
            )

        token = token_file.read_text().strip()
        server = server_file.read_text().strip() if server_file.exists() else api_url

        if not server:
            raise ValueError(
                f'No API server URL for cluster "{cluster_name}". '
                f'Save it to {server_file}'
            )

        configuration = k8s_client.Configuration()
        configuration.host = server
        configuration.api_key = {'authorization': f'Bearer {token}'}
        configuration.verify_ssl = False
        return k8s_client.ApiClient(configuration)

    async def _get_dynamic_client(self, cluster_id: str):
        """Get a dynamic kubernetes client for arbitrary resource types."""
        api_client = await self._get_k8s_client(cluster_id)
        return dynamic.DynamicClient(api_client), api_client


    async def rosa_top_pods(
        self,
        ctx: Context,
        cluster_id: str,
        namespace: Optional[str] = None,
        sort_by: str = 'cpu',
    ) -> list[TextContent]:
        """Get real-time CPU and memory usage for pods (like 'oc adm top pods').

        Queries the metrics.k8s.io API (metrics-server) for instantaneous resource
        consumption. Does NOT require CloudWatch Container Insights.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
            namespace: Namespace to query. If omitted, queries all namespaces.
            sort_by: Sort results by 'cpu' or 'memory'. Default: 'cpu'.
        """
        dyn_client, api_client = await self._get_dynamic_client(cluster_id)

        try:
            metrics_api = dyn_client.resources.get(
                api_version='metrics.k8s.io/v1beta1', kind='PodMetrics'
            )

            if namespace:
                result = metrics_api.get(namespace=namespace)
            else:
                result = metrics_api.get()

            pods = []
            for item in result.items or []:
                pod_name = item.metadata.name
                pod_ns = item.metadata.namespace
                total_cpu = 0
                total_mem = 0
                for container in item.containers or []:
                    usage = container.get('usage', {})
                    cpu_str = usage.get('cpu', '0')
                    mem_str = usage.get('memory', '0')
                    total_cpu += self._parse_cpu(cpu_str)
                    total_mem += self._parse_memory(mem_str)
                pods.append({
                    'namespace': pod_ns,
                    'name': pod_name,
                    'cpu_millicores': total_cpu,
                    'memory_mib': round(total_mem / (1024 * 1024), 1),
                })

            sort_key = 'cpu_millicores' if sort_by == 'cpu' else 'memory_mib'
            pods.sort(key=lambda p: p[sort_key], reverse=True)

            return [TextContent(
                type='text',
                text=json.dumps({'pod_count': len(pods), 'pods': pods[:50]}, indent=2),
            )]
        finally:
            api_client.close() if hasattr(api_client, 'close') else None


    async def rosa_top_nodes(
        self,
        ctx: Context,
        cluster_id: str,
        sort_by: str = 'cpu',
    ) -> list[TextContent]:
        """Get real-time CPU and memory usage for nodes (like 'oc adm top nodes').

        Queries the metrics.k8s.io API (metrics-server) for instantaneous node
        resource consumption. Shows allocatable vs used.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
            sort_by: Sort results by 'cpu' or 'memory'. Default: 'cpu'.
        """
        dyn_client, api_client = await self._get_dynamic_client(cluster_id)

        try:
            # Get node metrics
            metrics_api = dyn_client.resources.get(
                api_version='metrics.k8s.io/v1beta1', kind='NodeMetrics'
            )
            metrics_result = metrics_api.get()

            # Get node allocatable info
            core_v1 = k8s_client.CoreV1Api(api_client)
            nodes_list = core_v1.list_node()
            allocatable_map = {}
            for node in nodes_list.items:
                alloc = node.status.allocatable or {}
                allocatable_map[node.metadata.name] = {
                    'cpu': self._parse_cpu(alloc.get('cpu', '0')),
                    'memory': self._parse_memory(alloc.get('memory', '0')),
                }

            nodes = []
            for item in metrics_result.items or []:
                node_name = item.metadata.name
                usage = item.get('usage', {})
                cpu_used = self._parse_cpu(usage.get('cpu', '0'))
                mem_used = self._parse_memory(usage.get('memory', '0'))
                alloc = allocatable_map.get(node_name, {'cpu': 0, 'memory': 0})
                cpu_pct = round((cpu_used / alloc['cpu'] * 100), 1) if alloc['cpu'] else 0
                mem_pct = round((mem_used / alloc['memory'] * 100), 1) if alloc['memory'] else 0
                nodes.append({
                    'name': node_name,
                    'cpu_used_millicores': cpu_used,
                    'cpu_allocatable_millicores': alloc['cpu'],
                    'cpu_percent': cpu_pct,
                    'memory_used_mib': round(mem_used / (1024 * 1024), 1),
                    'memory_allocatable_mib': round(alloc['memory'] / (1024 * 1024), 1),
                    'memory_percent': mem_pct,
                })

            sort_key = 'cpu_percent' if sort_by == 'cpu' else 'memory_percent'
            nodes.sort(key=lambda n: n[sort_key], reverse=True)

            return [TextContent(
                type='text',
                text=json.dumps({'node_count': len(nodes), 'nodes': nodes}, indent=2),
            )]
        finally:
            api_client.close() if hasattr(api_client, 'close') else None


    async def rosa_list_routes(
        self,
        ctx: Context,
        cluster_id: str,
        namespace: Optional[str] = None,
        label_selector: Optional[str] = None,
    ) -> list[TextContent]:
        """List OpenShift Routes (the OpenShift-native equivalent of Ingress).

        Returns route host, path, TLS config, target service, and status.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
            namespace: Namespace to list routes from. If omitted, lists all namespaces.
            label_selector: Label selector to filter routes.
        """
        dyn_client, api_client = await self._get_dynamic_client(cluster_id)

        try:
            route_api = dyn_client.resources.get(
                api_version='route.openshift.io/v1', kind='Route'
            )

            kwargs = {}
            if label_selector:
                kwargs['label_selector'] = label_selector

            if namespace:
                result = route_api.get(namespace=namespace, **kwargs)
            else:
                result = route_api.get(**kwargs)

            routes = []
            for item in result.items or []:
                spec = item.get('spec', {})
                status = item.get('status', {})
                ingress_list = status.get('ingress', [])
                admitted = any(
                    c.get('type') == 'Admitted' and c.get('status') == 'True'
                    for ing in ingress_list
                    for c in (ing.get('conditions') or [])
                )
                routes.append({
                    'namespace': item.metadata.namespace,
                    'name': item.metadata.name,
                    'host': spec.get('host', ''),
                    'path': spec.get('path', '/'),
                    'tls_termination': (spec.get('tls') or {}).get('termination', 'none'),
                    'target_service': (spec.get('to') or {}).get('name', ''),
                    'target_port': (spec.get('port') or {}).get('targetPort', ''),
                    'admitted': admitted,
                })

            return [TextContent(
                type='text',
                text=json.dumps({'route_count': len(routes), 'routes': routes}, indent=2),
            )]
        finally:
            api_client.close() if hasattr(api_client, 'close') else None


    async def rosa_get_quotas(
        self,
        ctx: Context,
        cluster_id: str,
        namespace: Optional[str] = None,
    ) -> list[TextContent]:
        """Get ResourceQuotas and LimitRanges for a namespace or all namespaces.

        Essential for understanding resource constraints that affect HPA and
        right-sizing decisions. Shows hard limits, current usage, and default
        request/limit values.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
            namespace: Namespace to query. If omitted, queries all namespaces.
        """
        api_client = await self._get_k8s_client(cluster_id)

        try:
            core_v1 = k8s_client.CoreV1Api(api_client)

            # ResourceQuotas
            if namespace:
                quotas = core_v1.list_namespaced_resource_quota(namespace)
            else:
                quotas = core_v1.list_resource_quota_for_all_namespaces()

            quota_list = []
            for q in quotas.items:
                status = q.status or k8s_client.V1ResourceQuotaStatus()
                quota_list.append({
                    'namespace': q.metadata.namespace,
                    'name': q.metadata.name,
                    'hard': api_client.sanitize_for_serialization(status.hard) or {},
                    'used': api_client.sanitize_for_serialization(status.used) or {},
                })

            # LimitRanges
            if namespace:
                limit_ranges = core_v1.list_namespaced_limit_range(namespace)
            else:
                limit_ranges = core_v1.list_limit_range_for_all_namespaces()

            lr_list = []
            for lr in limit_ranges.items:
                limits = []
                for limit in (lr.spec.limits or []):
                    limits.append({
                        'type': limit.type,
                        'default': api_client.sanitize_for_serialization(limit.default) or {},
                        'defaultRequest': api_client.sanitize_for_serialization(
                            limit.default_request
                        ) or {},
                        'max': api_client.sanitize_for_serialization(limit.max) or {},
                        'min': api_client.sanitize_for_serialization(limit.min) or {},
                    })
                lr_list.append({
                    'namespace': lr.metadata.namespace,
                    'name': lr.metadata.name,
                    'limits': limits,
                })

            return [TextContent(
                type='text',
                text=json.dumps({
                    'resource_quotas': quota_list,
                    'limit_ranges': lr_list,
                }, indent=2),
            )]
        finally:
            api_client.close() if hasattr(api_client, 'close') else None


    async def rosa_list_projects(
        self,
        ctx: Context,
        cluster_id: str,
    ) -> list[TextContent]:
        """List OpenShift Projects (namespace + RBAC annotations).

        Projects are the OpenShift abstraction over namespaces, including
        display name, description, requester, and status.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
        """
        dyn_client, api_client = await self._get_dynamic_client(cluster_id)

        try:
            project_api = dyn_client.resources.get(
                api_version='project.openshift.io/v1', kind='Project'
            )
            result = project_api.get()

            projects = []
            for item in result.items or []:
                annotations = item.metadata.get('annotations', {}) or {}
                projects.append({
                    'name': item.metadata.name,
                    'display_name': annotations.get(
                        'openshift.io/display-name', ''
                    ),
                    'description': annotations.get(
                        'openshift.io/description', ''
                    ),
                    'requester': annotations.get(
                        'openshift.io/requester', ''
                    ),
                    'status': (item.get('status') or {}).get('phase', 'Active'),
                })

            # Filter out system projects for cleaner output
            user_projects = [p for p in projects if not p['name'].startswith('openshift-')
                            and not p['name'].startswith('kube-')]

            return [TextContent(
                type='text',
                text=json.dumps({
                    'total_projects': len(projects),
                    'user_projects': len(user_projects),
                    'projects': user_projects,
                    'system_project_names': [p['name'] for p in projects
                                             if p not in user_projects],
                }, indent=2),
            )]
        finally:
            api_client.close() if hasattr(api_client, 'close') else None


    async def rosa_rollout_status(
        self,
        ctx: Context,
        cluster_id: str,
        name: str,
        namespace: str = 'default',
        kind: str = 'Deployment',
    ) -> list[TextContent]:
        """Get rollout status and history for a Deployment or DeploymentConfig.

        Shows current replica counts, conditions, and revision history.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
            name: Name of the Deployment or DeploymentConfig.
            namespace: Namespace (default: 'default').
            kind: 'Deployment' or 'DeploymentConfig'. Default: 'Deployment'.
        """
        dyn_client, api_client = await self._get_dynamic_client(cluster_id)

        try:
            if kind.lower() == 'deploymentconfig':
                api_version = 'apps.openshift.io/v1'
                resource_kind = 'DeploymentConfig'
            else:
                api_version = 'apps/v1'
                resource_kind = 'Deployment'

            resource_api = dyn_client.resources.get(
                api_version=api_version, kind=resource_kind
            )
            result = resource_api.get(name=name, namespace=namespace)
            data = result.to_dict() if hasattr(result, 'to_dict') else {}

            spec = data.get('spec', {})
            status = data.get('status', {})
            conditions = status.get('conditions', [])

            rollout_info = {
                'name': name,
                'namespace': namespace,
                'kind': resource_kind,
                'replicas_desired': spec.get('replicas', 0),
                'replicas_ready': status.get('readyReplicas', 0),
                'replicas_available': status.get('availableReplicas', 0),
                'replicas_updated': status.get('updatedReplicas', 0),
                'observed_generation': status.get('observedGeneration', 0),
                'conditions': [
                    {
                        'type': c.get('type', ''),
                        'status': c.get('status', ''),
                        'reason': c.get('reason', ''),
                        'message': c.get('message', ''),
                        'lastUpdateTime': c.get('lastUpdateTime', ''),
                    }
                    for c in conditions
                ],
            }

            # Determine overall status
            ready = status.get('readyReplicas', 0)
            desired = spec.get('replicas', 0)
            if ready == desired and desired > 0:
                rollout_info['rollout_status'] = 'Complete'
            elif status.get('updatedReplicas', 0) < desired:
                rollout_info['rollout_status'] = 'Progressing'
            else:
                rollout_info['rollout_status'] = 'Waiting'

            return [TextContent(
                type='text',
                text=json.dumps(rollout_info, indent=2),
            )]
        finally:
            api_client.close() if hasattr(api_client, 'close') else None



    async def rosa_list_hpas(
        self,
        ctx: Context,
        cluster_id: str,
        namespace: Optional[str] = None,
    ) -> list[TextContent]:
        """List HorizontalPodAutoscalers with current vs target metrics.

        Shows min/max/current replicas, scaling metrics (CPU/memory/custom),
        target utilization vs current utilization, and scaling conditions.
        Essential for understanding autoscaling behavior and right-sizing.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
            namespace: Namespace to query. If omitted, queries all namespaces.
        """
        dyn_client, api_client = await self._get_dynamic_client(cluster_id)

        try:
            hpa_api = dyn_client.resources.get(
                api_version='autoscaling/v2', kind='HorizontalPodAutoscaler'
            )

            if namespace:
                result = hpa_api.get(namespace=namespace)
            else:
                result = hpa_api.get()

            hpas = []
            for item in result.items or []:
                spec = item.get('spec', {})
                status = item.get('status', {})
                metrics_spec = spec.get('metrics', [])
                metrics_status = status.get('currentMetrics', [])

                metrics = []
                for i, m in enumerate(metrics_spec):
                    metric_info = {'type': m.get('type', '')}
                    if m.get('type') == 'Resource':
                        res = m.get('resource', {})
                        metric_info['resource'] = res.get('name', '')
                        target = res.get('target', {})
                        metric_info['target_type'] = target.get('type', '')
                        metric_info['target_value'] = target.get('averageUtilization') or target.get('averageValue', '')
                        if i < len(metrics_status):
                            curr = metrics_status[i].get('resource', {})
                            current = curr.get('current', {})
                            metric_info['current_value'] = current.get('averageUtilization') or current.get('averageValue', '')
                    elif m.get('type') == 'Pods':
                        pods_m = m.get('pods', {})
                        metric_info['metric_name'] = pods_m.get('metric', {}).get('name', '')
                        metric_info['target_value'] = pods_m.get('target', {}).get('averageValue', '')
                    metrics.append(metric_info)

                conditions = [
                    {
                        'type': c.get('type', ''),
                        'status': c.get('status', ''),
                        'reason': c.get('reason', ''),
                    }
                    for c in (status.get('conditions') or [])
                ]

                hpas.append({
                    'namespace': item.metadata.namespace,
                    'name': item.metadata.name,
                    'target_ref': f"{spec.get('scaleTargetRef', {}).get('kind', '')}/{spec.get('scaleTargetRef', {}).get('name', '')}",
                    'min_replicas': spec.get('minReplicas', 1),
                    'max_replicas': spec.get('maxReplicas', 0),
                    'current_replicas': status.get('currentReplicas', 0),
                    'desired_replicas': status.get('desiredReplicas', 0),
                    'metrics': metrics,
                    'conditions': conditions,
                })

            return [TextContent(
                type='text',
                text=json.dumps({'hpa_count': len(hpas), 'hpas': hpas}, indent=2),
            )]
        finally:
            api_client.close() if hasattr(api_client, 'close') else None

    async def rosa_right_size_report(
        self,
        ctx: Context,
        cluster_id: str,
        namespace: Optional[str] = None,
        threshold_cpu_percent: float = 30.0,
        threshold_memory_percent: float = 30.0,
    ) -> list[TextContent]:
        """Generate a right-sizing report comparing actual usage vs requests/limits.

        For each Deployment in the namespace, compares real-time metrics (from
        metrics-server) against configured requests and limits. Flags workloads
        that are over-provisioned (using <threshold% of requests) or under-provisioned
        (using >90% of limits).

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
            namespace: Namespace to analyze. If omitted, analyzes all user namespaces.
            threshold_cpu_percent: CPU utilization below this % of request = over-provisioned. Default: 30.
            threshold_memory_percent: Memory utilization below this % of request = over-provisioned. Default: 30.
        """
        dyn_client, api_client = await self._get_dynamic_client(cluster_id)

        try:
            apps_api = k8s_client.AppsV1Api(api_client)
            metrics_api = dyn_client.resources.get(
                api_version='metrics.k8s.io/v1beta1', kind='PodMetrics'
            )

            if namespace:
                deployments = apps_api.list_namespaced_deployment(namespace)
            else:
                deployments = apps_api.list_deployment_for_all_namespaces()

            if namespace:
                pod_metrics = metrics_api.get(namespace=namespace)
            else:
                pod_metrics = metrics_api.get()

            # Build metrics map
            pod_usage = {}
            for item in pod_metrics.items or []:
                pod_key = f"{item.metadata.namespace}/{item.metadata.name}"
                total_cpu = 0
                total_mem = 0
                for container in item.containers or []:
                    usage = container.get('usage', {})
                    total_cpu += self._parse_cpu(usage.get('cpu', '0'))
                    total_mem += self._parse_memory(usage.get('memory', '0'))
                pod_usage[pod_key] = {'cpu': total_cpu, 'memory': total_mem}

            report = []
            for deploy in deployments.items:
                ns = deploy.metadata.namespace
                name = deploy.metadata.name

                if ns.startswith('openshift-') or ns.startswith('kube-'):
                    continue

                containers = deploy.spec.template.spec.containers or []
                total_request_cpu = 0
                total_request_mem = 0
                total_limit_cpu = 0
                total_limit_mem = 0

                for c in containers:
                    resources = c.resources or k8s_client.V1ResourceRequirements()
                    requests = resources.requests or {}
                    limits = resources.limits or {}
                    total_request_cpu += self._parse_cpu(requests.get('cpu', '0'))
                    total_request_mem += self._parse_memory(requests.get('memory', '0'))
                    total_limit_cpu += self._parse_cpu(limits.get('cpu', '0'))
                    total_limit_mem += self._parse_memory(limits.get('memory', '0'))

                # Match pods by deployment name prefix
                matching_pods_usage = []
                for pod_key, usage in pod_usage.items():
                    if pod_key.startswith(f"{ns}/") and name in pod_key:
                        matching_pods_usage.append(usage)

                if not matching_pods_usage:
                    continue

                avg_cpu = sum(p['cpu'] for p in matching_pods_usage) / len(matching_pods_usage)
                avg_mem = sum(p['memory'] for p in matching_pods_usage) / len(matching_pods_usage)

                cpu_util_of_request = (avg_cpu / total_request_cpu * 100) if total_request_cpu else 0
                mem_util_of_request = (avg_mem / total_request_mem * 100) if total_request_mem else 0
                cpu_util_of_limit = (avg_cpu / total_limit_cpu * 100) if total_limit_cpu else 0
                mem_util_of_limit = (avg_mem / total_limit_mem * 100) if total_limit_mem else 0

                issues = []
                if total_request_cpu > 0 and cpu_util_of_request < threshold_cpu_percent:
                    issues.append(f'CPU over-provisioned ({cpu_util_of_request:.0f}% of request)')
                if total_request_mem > 0 and mem_util_of_request < threshold_memory_percent:
                    issues.append(f'Memory over-provisioned ({mem_util_of_request:.0f}% of request)')
                if total_limit_cpu > 0 and cpu_util_of_limit > 90:
                    issues.append(f'CPU near limit ({cpu_util_of_limit:.0f}% of limit)')
                if total_limit_mem > 0 and mem_util_of_limit > 90:
                    issues.append(f'Memory near limit ({mem_util_of_limit:.0f}% of limit)')
                if total_request_cpu == 0:
                    issues.append('No CPU request set')
                if total_request_mem == 0:
                    issues.append('No memory request set')

                status = 'optimal' if not issues else ('critical' if any('near limit' in i for i in issues) else 'over-provisioned')

                report.append({
                    'namespace': ns,
                    'deployment': name,
                    'replicas': deploy.spec.replicas,
                    'pod_count_with_metrics': len(matching_pods_usage),
                    'requests': {
                        'cpu_millicores': total_request_cpu,
                        'memory_mib': round(total_request_mem / (1024 * 1024), 1),
                    },
                    'limits': {
                        'cpu_millicores': total_limit_cpu,
                        'memory_mib': round(total_limit_mem / (1024 * 1024), 1),
                    },
                    'actual_usage_avg': {
                        'cpu_millicores': round(avg_cpu, 1),
                        'memory_mib': round(avg_mem / (1024 * 1024), 1),
                    },
                    'utilization': {
                        'cpu_percent_of_request': round(cpu_util_of_request, 1),
                        'memory_percent_of_request': round(mem_util_of_request, 1),
                        'cpu_percent_of_limit': round(cpu_util_of_limit, 1),
                        'memory_percent_of_limit': round(mem_util_of_limit, 1),
                    },
                    'status': status,
                    'issues': issues,
                    'recommendation': {
                        'cpu_request': f'{max(int(avg_cpu * 1.3), 10)}m' if total_request_cpu > 0 and cpu_util_of_request < threshold_cpu_percent else 'keep',
                        'memory_request': f'{max(int(avg_mem * 1.3 / (1024 * 1024)), 32)}Mi' if total_request_mem > 0 and mem_util_of_request < threshold_memory_percent else 'keep',
                    },
                })

            priority = {'critical': 0, 'over-provisioned': 1, 'optimal': 2}
            report.sort(key=lambda r: priority.get(r['status'], 3))

            summary = {
                'total_deployments_analyzed': len(report),
                'critical': sum(1 for r in report if r['status'] == 'critical'),
                'over_provisioned': sum(1 for r in report if r['status'] == 'over-provisioned'),
                'optimal': sum(1 for r in report if r['status'] == 'optimal'),
            }

            return [TextContent(
                type='text',
                text=json.dumps({
                    'summary': summary,
                    'thresholds': {
                        'cpu_over_provisioned_below': threshold_cpu_percent,
                        'memory_over_provisioned_below': threshold_memory_percent,
                    },
                    'deployments': report,
                }, indent=2),
            )]
        finally:
            api_client.close() if hasattr(api_client, 'close') else None

    async def rosa_list_ack_controllers(
        self,
        ctx: Context,
        cluster_id: str,
    ) -> list[TextContent]:
        """List ACK (AWS Controllers for Kubernetes) controllers installed in the cluster.

        Shows which AWS service controllers are deployed, their status, and version.
        ACK controllers run in the 'ack-system' namespace by default.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
        """
        api_client = await self._get_k8s_client(cluster_id)

        try:
            apps_api = k8s_client.AppsV1Api(api_client)
            deployments = apps_api.list_namespaced_deployment('ack-system')

            controllers = []
            for deploy in deployments.items:
                name = deploy.metadata.name
                status = deploy.status
                containers = deploy.spec.template.spec.containers or []
                image = containers[0].image if containers else ''

                controllers.append({
                    'name': name,
                    'service': name.replace('ack-', '').replace('-controller', ''),
                    'ready_replicas': status.ready_replicas or 0,
                    'desired_replicas': deploy.spec.replicas or 0,
                    'image': image,
                    'available': (status.ready_replicas or 0) >= (deploy.spec.replicas or 1),
                })

            # Also discover CRDs from services.k8s.aws
            dyn_client = dynamic.DynamicClient(api_client)
            crd_api = dyn_client.resources.get(
                api_version='apiextensions.k8s.io/v1',
                kind='CustomResourceDefinition',
            )
            crd_result = crd_api.get()

            services_discovered = {}
            for crd in crd_result.items or []:
                crd_name = crd.metadata.name
                if 'services.k8s.aws' in crd_name:
                    # Extract service: e.g. "tables.dynamodb.services.k8s.aws" -> "dynamodb"
                    parts = crd_name.split('.')
                    if len(parts) >= 3:
                        svc = parts[1]
                        if svc not in services_discovered:
                            services_discovered[svc] = []
                        services_discovered[svc].append(parts[0])

            return [TextContent(
                type='text',
                text=json.dumps({
                    'controller_count': len(controllers),
                    'controllers': controllers,
                    'ack_services_with_crds': {
                        svc: sorted(kinds) for svc, kinds in sorted(services_discovered.items())
                    },
                }, indent=2),
            )]
        finally:
            api_client.close() if hasattr(api_client, 'close') else None

    async def rosa_list_ack_resources(
        self,
        ctx: Context,
        cluster_id: str,
        service: str,
        kind: Optional[str] = None,
        namespace: Optional[str] = None,
    ) -> list[TextContent]:
        """List AWS resources managed by ACK for a given AWS service.

        Dynamically discovers CRDs for the specified service and lists all
        resources of that type. Works for any ACK service: ec2, dynamodb,
        s3, rds, documentdb, cognitoidentityprovider, etc.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
            service: AWS service name (e.g. 'ec2', 'dynamodb', 's3', 'rds', 'documentdb').
            kind: Specific resource kind to list (e.g. 'VPC', 'Table'). If omitted, lists all kinds for the service.
            namespace: Namespace filter. If omitted, lists across all namespaces.
        """
        dyn_client, api_client = await self._get_dynamic_client(cluster_id)

        try:
            # Discover CRDs for this service
            crd_api = dyn_client.resources.get(
                api_version='apiextensions.k8s.io/v1',
                kind='CustomResourceDefinition',
            )
            crd_result = crd_api.get()

            api_group = f'{service}.services.k8s.aws'
            matching_crds = []
            for crd in crd_result.items or []:
                crd_name = crd.metadata.name
                if crd_name.endswith(api_group):
                    spec = crd.get('spec', {})
                    crd_kind = spec.get('names', {}).get('kind', '')
                    versions = spec.get('versions', [])
                    version = versions[0].get('name', 'v1alpha1') if versions else 'v1alpha1'
                    matching_crds.append({
                        'kind': crd_kind,
                        'api_version': f'{api_group}/{version}',
                        'plural': spec.get('names', {}).get('plural', ''),
                    })

            if not matching_crds:
                return [TextContent(
                    type='text',
                    text=json.dumps({
                        'error': f'No ACK CRDs found for service "{service}". '
                                 f'Expected CRDs matching *.{api_group}',
                        'hint': 'Use rosa_list_ack_controllers to see available services.',
                    }),
                )]

            # Filter by kind if specified
            if kind:
                matching_crds = [c for c in matching_crds if c['kind'].lower() == kind.lower()]
                if not matching_crds:
                    all_kinds = [c['kind'] for c in matching_crds]
                    return [TextContent(
                        type='text',
                        text=json.dumps({
                            'error': f'Kind "{kind}" not found for service "{service}".',
                            'available_kinds': all_kinds,
                        }),
                    )]

            # List resources for each CRD
            all_resources = []
            for crd_info in matching_crds:
                try:
                    resource_api = dyn_client.resources.get(
                        api_version=crd_info['api_version'],
                        kind=crd_info['kind'],
                    )

                    if namespace:
                        result = resource_api.get(namespace=namespace)
                    else:
                        result = resource_api.get()

                    for item in result.items or []:
                        status = item.get('status', {})
                        conditions = status.get('conditions', [])
                        synced = any(
                            c.get('type') == 'ACK.ResourceSynced' and c.get('status') == 'True'
                            for c in conditions
                        )
                        all_resources.append({
                            'kind': crd_info['kind'],
                            'namespace': item.metadata.namespace,
                            'name': item.metadata.name,
                            'synced': synced,
                            'ack_resource_id': status.get('ackResourceMetadata', {}).get('arn', ''),
                            'conditions': [
                                {'type': c.get('type', ''), 'status': c.get('status', ''), 'message': c.get('message', '')}
                                for c in conditions
                            ],
                        })
                except Exception as e:
                    all_resources.append({
                        'kind': crd_info['kind'],
                        'error': str(e),
                    })

            return [TextContent(
                type='text',
                text=json.dumps({
                    'service': service,
                    'api_group': api_group,
                    'available_kinds': [c['kind'] for c in matching_crds],
                    'resource_count': len([r for r in all_resources if 'error' not in r]),
                    'resources': all_resources,
                }, indent=2),
            )]
        finally:
            api_client.close() if hasattr(api_client, 'close') else None

    @staticmethod
    def _parse_cpu(cpu_str: str) -> int:
        """Parse CPU string to millicores (int).

        Handles: '100m' -> 100, '1' -> 1000, '2500m' -> 2500, '0' -> 0.
        """
        if not cpu_str:
            return 0
        cpu_str = str(cpu_str).strip()
        if cpu_str.endswith('n'):
            return int(int(cpu_str[:-1]) / 1_000_000)
        elif cpu_str.endswith('u'):
            return int(int(cpu_str[:-1]) / 1_000)
        elif cpu_str.endswith('m'):
            return int(cpu_str[:-1])
        else:
            return int(float(cpu_str) * 1000)

    @staticmethod
    def _parse_memory(mem_str: str) -> int:
        """Parse memory string to bytes (int).

        Handles: '128Mi' -> bytes, '1Gi' -> bytes, '1000Ki' -> bytes, '1073741824' -> bytes.
        """
        if not mem_str:
            return 0
        mem_str = str(mem_str).strip()
        multipliers = {
            'Ki': 1024,
            'Mi': 1024 ** 2,
            'Gi': 1024 ** 3,
            'Ti': 1024 ** 4,
            'K': 1000,
            'M': 1000 ** 2,
            'G': 1000 ** 3,
            'T': 1000 ** 4,
        }
        for suffix, mult in multipliers.items():
            if mem_str.endswith(suffix):
                return int(float(mem_str[:-len(suffix)]) * mult)
        return int(mem_str)
