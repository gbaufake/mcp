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

"""ROSA cluster lifecycle handler using the OCM REST API."""

import json
from awslabs.rosa_mcp_server.ocm_client import OCMClient
from mcp.server.fastmcp import Context
from mcp.types import TextContent
from typing import Optional


class RosaClusterHandler:
    """Handler for ROSA cluster lifecycle operations via OCM API."""

    def __init__(self, mcp, ocm_client: OCMClient, allow_write: bool = False):
        """Initialize the handler.

        Args:
            mcp: The FastMCP server instance.
            ocm_client: Shared OCM API client.
            allow_write: Whether write operations are permitted.
        """
        self.mcp = mcp
        self.ocm = ocm_client
        self.allow_write = allow_write

        self.mcp.tool(name='rosa_list_clusters')(self.rosa_list_clusters)
        self.mcp.tool(name='rosa_describe_cluster')(self.rosa_describe_cluster)
        self.mcp.tool(name='rosa_create_cluster')(self.rosa_create_cluster)
        self.mcp.tool(name='rosa_delete_cluster')(self.rosa_delete_cluster)
        self.mcp.tool(name='rosa_list_versions')(self.rosa_list_versions)
        self.mcp.tool(name='rosa_list_upgrades')(self.rosa_list_upgrades)
        self.mcp.tool(name='rosa_upgrade_cluster')(self.rosa_upgrade_cluster)
        self.mcp.tool(name='rosa_get_cluster_credentials')(self.rosa_get_cluster_credentials)
        self.mcp.tool(name='rosa_get_install_logs')(self.rosa_get_install_logs)

    async def rosa_list_clusters(
        self,
        ctx: Context,
        search: Optional[str] = None,
    ) -> list[TextContent]:
        """List all ROSA clusters in the current account.

        Args:
            ctx: MCP context.
            search: Optional OCM search filter (e.g., "state = 'ready'", "name like 'prod%'").
        """
        data = await self.ocm.list_clusters(search=search or "product.id = 'rosa'")
        return [TextContent(type='text', text=json.dumps(data, indent=2))]

    async def rosa_describe_cluster(
        self,
        ctx: Context,
        cluster_id: str,
    ) -> list[TextContent]:
        """Get detailed information about a specific ROSA cluster.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
        """
        data = await self.ocm.get_cluster(cluster_id)
        return [TextContent(type='text', text=json.dumps(data, indent=2))]

    async def rosa_create_cluster(
        self,
        ctx: Context,
        name: str,
        region: str,
        aws_account_id: str,
        version: Optional[str] = None,
        multi_az: bool = False,
        compute_nodes: int = 2,
        compute_machine_type: str = 'm5.xlarge',
        pod_cidr: str = '10.128.0.0/14',
        service_cidr: str = '172.30.0.0/16',
        machine_cidr: str = '10.0.0.0/16',
        host_prefix: int = 23,
        private: bool = False,
        subnet_ids: Optional[list[str]] = None,
        availability_zones: Optional[list[str]] = None,
        installer_role_arn: Optional[str] = None,
        support_role_arn: Optional[str] = None,
        worker_role_arn: Optional[str] = None,
        operator_role_prefix: Optional[str] = None,
        oidc_config_id: Optional[str] = None,
        billing_account_id: Optional[str] = None,
        ec2_metadata_http_tokens: str = 'required',
        etcd_encryption: bool = False,
        etcd_encryption_kms_arn: Optional[str] = None,
        kms_key_arn: Optional[str] = None,
        fips: bool = False,
        disable_workload_monitoring: bool = False,
        worker_disk_size: Optional[int] = None,
        additional_compute_security_group_ids: Optional[list[str]] = None,
        audit_log_arn: Optional[str] = None,
        channel: Optional[str] = None,
        tags: Optional[dict[str, str]] = None,
    ) -> list[TextContent]:
        """Create a new ROSA HCP (Hosted Control Plane) cluster via OCM API.

        Args:
            ctx: MCP context.
            name: Cluster name (2-54 chars, lowercase alphanumeric/hyphens).
            region: AWS region (e.g., sa-east-1).
            aws_account_id: 12-digit AWS account ID.
            version: OpenShift version (e.g., '4.20.29'). Omit for latest.
            multi_az: Deploy across multiple availability zones.
            compute_nodes: Number of worker nodes.
            compute_machine_type: EC2 instance type for workers.
            pod_cidr: CIDR for pod network.
            service_cidr: CIDR for service network.
            machine_cidr: CIDR for machine network.
            host_prefix: Host prefix length for pod CIDR allocation per node.
            private: Make cluster private (PrivateLink).
            subnet_ids: VPC subnet IDs (required for HCP BYO VPC).
            availability_zones: Specific AZs to deploy to.
            installer_role_arn: ARN of the HCP ROSA installer IAM role.
            support_role_arn: ARN of the HCP ROSA support IAM role.
            worker_role_arn: ARN of the HCP ROSA worker IAM role.
            operator_role_prefix: Prefix for operator IAM roles.
            oidc_config_id: OIDC config ID for STS mode.
            billing_account_id: AWS billing account ID (marketplace billing).
            ec2_metadata_http_tokens: IMDS mode ('required' or 'optional'). Default: required.
            etcd_encryption: Enable etcd encryption.
            etcd_encryption_kms_arn: KMS key ARN for etcd encryption.
            kms_key_arn: KMS key ARN for EBS volume encryption.
            fips: Enable FIPS mode.
            disable_workload_monitoring: Disable user workload monitoring.
            worker_disk_size: Root disk size in GiB for worker nodes.
            additional_compute_security_group_ids: Additional security group IDs for workers.
            audit_log_arn: IAM role ARN for CloudWatch audit log forwarding.
            channel: Version channel (e.g., 'stable-4.20', 'eus-4.20').
            tags: AWS resource tags.
        """
        if not self.allow_write:
            raise ValueError(
                'Write operations disabled. Start the server with --allow-write.'
            )

        body: dict = {
            'name': name,
            'product': {'id': 'rosa'},
            'cloud_provider': {'id': 'aws'},
            'region': {'id': region},
            'multi_az': multi_az,
            'hypershift': {'enabled': True},
            'ccs': {'enabled': True},
            'aws': {
                'account_id': aws_account_id,
                'ec2_metadata_http_tokens': ec2_metadata_http_tokens,
            },
            'nodes': {
                'compute': compute_nodes,
                'compute_machine_type': {'id': compute_machine_type},
            },
            'network': {
                'type': 'OVNKubernetes',
                'pod_cidr': pod_cidr,
                'service_cidr': service_cidr,
                'machine_cidr': machine_cidr,
                'host_prefix': host_prefix,
            },
            'fips': fips,
            'etcd_encryption': etcd_encryption,
            'disable_user_workload_monitoring': disable_workload_monitoring,
        }

        # Version and channel
        if version:
            version_body: dict = {'id': f'openshift-v{version}'}
            if channel:
                body['channel'] = channel
            else:
                version_body['channel_group'] = 'stable'
            body['version'] = version_body
        elif channel:
            body['channel'] = channel

        # Networking
        if subnet_ids:
            body['aws']['subnet_ids'] = subnet_ids

        if availability_zones:
            body['nodes']['availability_zones'] = availability_zones

        if private:
            body['api'] = {'listening': 'internal'}

        # Worker config
        if worker_disk_size:
            body['nodes']['compute_root_volume'] = {'aws': {'size': worker_disk_size}}

        if additional_compute_security_group_ids:
            body['aws']['additional_compute_security_group_ids'] = additional_compute_security_group_ids

        # Billing
        if billing_account_id:
            body['aws']['billing_account_id'] = billing_account_id

        # Encryption
        if kms_key_arn:
            body['aws']['kms_key_arn'] = kms_key_arn

        if etcd_encryption_kms_arn:
            body['aws']['etcd_encryption'] = {'kms_key_arn': etcd_encryption_kms_arn}
            body['etcd_encryption'] = True

        # Audit log
        if audit_log_arn:
            body['aws']['audit_log'] = {'role_arn': audit_log_arn}

        # Tags
        if tags:
            body['aws']['tags'] = tags

        # STS configuration (HCP)
        if installer_role_arn:
            sts_config: dict = {
                'enabled': True,
                'role_arn': installer_role_arn,
                'support_role_arn': support_role_arn or '',
                'instance_iam_roles': {
                    'worker_role_arn': worker_role_arn or '',
                },
            }

            if operator_role_prefix:
                sts_config['operator_role_prefix'] = operator_role_prefix

            if oidc_config_id:
                sts_config['oidc_config'] = {'id': oidc_config_id}

            body['aws']['sts'] = sts_config

        data = await self.ocm.create_cluster(body)
        return [TextContent(type='text', text=json.dumps(data, indent=2))]

    async def rosa_delete_cluster(
        self,
        ctx: Context,
        cluster_id: str,
        deprovision: bool = True,
    ) -> list[TextContent]:
        """Delete a ROSA cluster.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID to delete.
            deprovision: Whether to remove AWS infrastructure (default True).
        """
        if not self.allow_write:
            raise ValueError(
                'Write operations disabled. Start the server with --allow-write.'
            )

        status_code = await self.ocm.delete_cluster(cluster_id, deprovision=deprovision)
        return [TextContent(
            type='text',
            text=json.dumps({'status': 'deletion_initiated', 'http_status': status_code}),
        )]

    async def rosa_list_versions(
        self,
        ctx: Context,
        channel_group: str = 'stable',
        hosted_cp_only: bool = False,
    ) -> list[TextContent]:
        """List available ROSA OpenShift versions.

        Args:
            ctx: MCP context.
            channel_group: Version channel (stable, candidate, nightly).
            hosted_cp_only: Only show HCP-enabled versions.
        """
        search_parts = ["rosa_enabled = 'true'", "enabled = 'true'"]
        search_parts.append(f"channel_group = '{channel_group}'")
        if hosted_cp_only:
            search_parts.append("hosted_control_plane_enabled = 'true'")

        data = await self.ocm.list_versions(search=' AND '.join(search_parts))
        return [TextContent(type='text', text=json.dumps(data, indent=2))]

    async def rosa_list_upgrades(
        self,
        ctx: Context,
        cluster_id: str,
    ) -> list[TextContent]:
        """List available upgrades for a ROSA cluster.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
        """
        cluster = await self.ocm.get_cluster(cluster_id)
        current_version = cluster.get('version', {}).get('raw_id', 'unknown')
        available = cluster.get('version', {}).get('available_upgrades', [])
        result = {
            'cluster_id': cluster_id,
            'current_version': current_version,
            'available_upgrades': available,
        }
        return [TextContent(type='text', text=json.dumps(result, indent=2))]

    async def rosa_upgrade_cluster(
        self,
        ctx: Context,
        cluster_id: str,
        version: str,
        schedule: Optional[str] = None,
    ) -> list[TextContent]:
        """Schedule an upgrade for a ROSA cluster.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
            version: Target OpenShift version (e.g., '4.14.6').
            schedule: Cron expression for scheduled upgrade (e.g., '0 2 * * *').
                     If omitted, the upgrade runs immediately.
        """
        if not self.allow_write:
            raise ValueError(
                'Write operations disabled. Start the server with --allow-write.'
            )

        body: dict = {
            'version': version,
            'schedule_type': 'manual',
            'upgrade_type': 'OSD',
        }
        if schedule:
            body['schedule'] = schedule
            body['schedule_type'] = 'automatic'

        data = await self.ocm.create_upgrade_policy(cluster_id, body)
        return [TextContent(type='text', text=json.dumps(data, indent=2))]

    async def rosa_get_cluster_credentials(
        self,
        ctx: Context,
        cluster_id: str,
    ) -> list[TextContent]:
        """Get cluster credentials (kubeconfig and admin password).

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
        """
        data = await self.ocm.get_cluster_credentials(cluster_id)
        return [TextContent(type='text', text=json.dumps(data, indent=2))]

    async def rosa_get_install_logs(
        self,
        ctx: Context,
        cluster_id: str,
        tail: Optional[int] = None,
    ) -> list[TextContent]:
        """Get cluster installation logs.

        Args:
            ctx: MCP context.
            cluster_id: The OCM cluster ID.
            tail: Number of lines from end to return.
        """
        data = await self.ocm.get_install_logs(cluster_id, tail=tail)
        return [TextContent(type='text', text=json.dumps(data, indent=2))]
