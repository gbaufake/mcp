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

"""Tests for the OCM client."""

import pytest
import time
from awslabs.rosa_mcp_server.ocm_client import (
    API_PATH,
    OCM_API_URL,
    OCM_CLIENT_ID_DEFAULT,
    OCM_SSO_TOKEN_URL,
    OCMClient,
)
from unittest.mock import AsyncMock, MagicMock, patch


class TestOCMClientInitialization:
    """Tests for OCMClient initialization and token loading."""

    def test_given_offline_token_in_env_when_init_then_uses_env_token(self):
        """Test token loading from environment variable."""
        with patch.dict('os.environ', {'OCM_TOKEN': 'env-token', 'OCM_CLIENT_ID': ''}):
            with patch.object(OCMClient, '_load_ocm_config', return_value={}):
                client = OCMClient()
                assert client._offline_token == 'env-token'

    def test_given_offline_token_arg_when_init_then_uses_arg_token(self):
        """Test token loading from constructor argument takes priority."""
        with patch.dict('os.environ', {'OCM_TOKEN': 'env-token', 'OCM_CLIENT_ID': ''}):
            with patch.object(OCMClient, '_load_ocm_config', return_value={}):
                client = OCMClient(offline_token='arg-token')
                assert client._offline_token == 'arg-token'

    def test_given_token_in_config_file_when_no_env_then_uses_config_token(self):
        """Test token loading from config file when env var is not set."""
        config = {'refresh_token': 'config-token', 'client_id': 'config-client'}
        with patch.dict('os.environ', {'OCM_TOKEN': '', 'OCM_CLIENT_ID': ''}):
            with patch.object(OCMClient, '_load_ocm_config', return_value=config):
                client = OCMClient()
                assert client._offline_token == 'config-token'
                assert client._client_id == 'config-client'

    def test_given_no_token_anywhere_when_init_then_raises_value_error(self):
        """Test ValueError when no token is available."""
        with patch.dict('os.environ', {'OCM_TOKEN': '', 'OCM_CLIENT_ID': ''}):
            with patch.object(OCMClient, '_load_ocm_config', return_value={}):
                with pytest.raises(ValueError, match='OCM offline token required'):
                    OCMClient()

    def test_given_no_client_id_when_init_then_uses_default(self):
        """Test client_id fallback to default 'cloud-services'."""
        with patch.dict('os.environ', {'OCM_TOKEN': 'token', 'OCM_CLIENT_ID': ''}):
            with patch.object(OCMClient, '_load_ocm_config', return_value={}):
                client = OCMClient()
                assert client._client_id == OCM_CLIENT_ID_DEFAULT

    def test_given_client_id_in_env_when_init_then_uses_env_client_id(self):
        """Test client_id from OCM_CLIENT_ID env var."""
        with patch.dict('os.environ', {'OCM_TOKEN': 'token', 'OCM_CLIENT_ID': 'my-client'}):
            with patch.object(OCMClient, '_load_ocm_config', return_value={}):
                client = OCMClient()
                assert client._client_id == 'my-client'

    def test_given_custom_api_url_when_init_then_uses_custom_url(self):
        """Test custom API URL is used."""
        with patch.dict('os.environ', {'OCM_TOKEN': 'token', 'OCM_CLIENT_ID': ''}):
            with patch.object(OCMClient, '_load_ocm_config', return_value={}):
                client = OCMClient(api_url='https://custom.api.com/')
                assert client._api_url == 'https://custom.api.com'


class TestOCMClientTokenManagement:
    """Tests for token exchange and caching."""

    @pytest.mark.asyncio
    async def test_given_no_token_when_ensure_token_then_makes_post_request(self):
        """Test _ensure_token makes correct POST request to SSO."""
        with patch.dict('os.environ', {'OCM_TOKEN': 'offline-token', 'OCM_CLIENT_ID': ''}):
            with patch.object(OCMClient, '_load_ocm_config', return_value={}):
                client = OCMClient()

        mock_response = MagicMock()
        mock_response.json.return_value = {
            'access_token': 'new-access-token',
            'expires_in': 900,
        }
        mock_response.raise_for_status = MagicMock()

        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)
        client._http = mock_http_client

        token = await client._ensure_token()

        assert token == 'new-access-token'
        mock_http_client.post.assert_called_once_with(
            OCM_SSO_TOKEN_URL,
            data={
                'grant_type': 'refresh_token',
                'client_id': OCM_CLIENT_ID_DEFAULT,
                'refresh_token': 'offline-token',
            },
        )

    @pytest.mark.asyncio
    async def test_given_valid_cached_token_when_ensure_token_then_returns_cached(self):
        """Test _ensure_token uses cached token when not expired."""
        with patch.dict('os.environ', {'OCM_TOKEN': 'offline-token', 'OCM_CLIENT_ID': ''}):
            with patch.object(OCMClient, '_load_ocm_config', return_value={}):
                client = OCMClient()

        client._access_token = 'cached-token'
        client._token_expiry = time.time() + 600  # Expires in 10 minutes

        mock_http_client = AsyncMock()
        client._http = mock_http_client

        token = await client._ensure_token()

        assert token == 'cached-token'
        mock_http_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_given_expired_token_when_ensure_token_then_refreshes(self):
        """Test _ensure_token refreshes when token is expired."""
        with patch.dict('os.environ', {'OCM_TOKEN': 'offline-token', 'OCM_CLIENT_ID': ''}):
            with patch.object(OCMClient, '_load_ocm_config', return_value={}):
                client = OCMClient()

        client._access_token = 'old-token'
        client._token_expiry = time.time() - 10  # Already expired

        mock_response = MagicMock()
        mock_response.json.return_value = {
            'access_token': 'refreshed-token',
            'expires_in': 900,
        }
        mock_response.raise_for_status = MagicMock()

        mock_http_client = AsyncMock()
        mock_http_client.post = AsyncMock(return_value=mock_response)
        client._http = mock_http_client

        token = await client._ensure_token()

        assert token == 'refreshed-token'
        mock_http_client.post.assert_called_once()


class TestOCMClientHTTPMethods:
    """Tests for HTTP method helpers."""

    @pytest.fixture
    def client_with_token(self):
        """Create a client with a pre-set valid token."""
        with patch.dict('os.environ', {'OCM_TOKEN': 'offline-token', 'OCM_CLIENT_ID': ''}):
            with patch.object(OCMClient, '_load_ocm_config', return_value={}):
                client = OCMClient()
        client._access_token = 'test-token'
        client._token_expiry = time.time() + 600
        return client

    @pytest.mark.asyncio
    async def test_get_formats_url_correctly(self, client_with_token):
        """Test _get method formats URLs correctly."""
        mock_response = MagicMock()
        mock_response.json.return_value = {'items': []}
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.get = AsyncMock(return_value=mock_response)
        client_with_token._http = mock_http

        await client_with_token._get('/clusters', params={'page': 1})

        mock_http.get.assert_called_once()
        call_args = mock_http.get.call_args
        assert f'{OCM_API_URL}{API_PATH}/clusters' == call_args[0][0]
        assert call_args[1]['params'] == {'page': 1}

    @pytest.mark.asyncio
    async def test_post_formats_url_and_sends_body(self, client_with_token):
        """Test _post method formats URLs and sends JSON body."""
        mock_response = MagicMock()
        mock_response.json.return_value = {'id': 'new'}
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        client_with_token._http = mock_http

        body = {'name': 'test'}
        await client_with_token._post('/clusters', body)

        call_args = mock_http.post.call_args
        assert f'{OCM_API_URL}{API_PATH}/clusters' == call_args[0][0]
        assert call_args[1]['json'] == body

    @pytest.mark.asyncio
    async def test_patch_formats_url_and_sends_body(self, client_with_token):
        """Test _patch method formats URLs and sends JSON body."""
        mock_response = MagicMock()
        mock_response.json.return_value = {'id': 'updated'}
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.patch = AsyncMock(return_value=mock_response)
        client_with_token._http = mock_http

        body = {'replicas': 5}
        await client_with_token._patch('/clusters/c1', body)

        call_args = mock_http.patch.call_args
        assert f'{OCM_API_URL}{API_PATH}/clusters/c1' == call_args[0][0]
        assert call_args[1]['json'] == body

    @pytest.mark.asyncio
    async def test_delete_formats_url_correctly(self, client_with_token):
        """Test _delete method formats URLs correctly."""
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.raise_for_status = MagicMock()

        mock_http = AsyncMock()
        mock_http.delete = AsyncMock(return_value=mock_response)
        client_with_token._http = mock_http

        result = await client_with_token._delete('/clusters/c1', params={'deprovision': 'true'})

        call_args = mock_http.delete.call_args
        assert f'{OCM_API_URL}{API_PATH}/clusters/c1' == call_args[0][0]
        assert call_args[1]['params'] == {'deprovision': 'true'}
        assert result == 204


class TestOCMClientClusterMethods:
    """Tests for cluster-specific OCM client methods."""

    @pytest.fixture
    def client_with_mocks(self):
        """Create client with mocked HTTP methods."""
        with patch.dict('os.environ', {'OCM_TOKEN': 'offline-token', 'OCM_CLIENT_ID': ''}):
            with patch.object(OCMClient, '_load_ocm_config', return_value={}):
                client = OCMClient()
        client._get = AsyncMock()
        client._post = AsyncMock()
        client._patch = AsyncMock()
        client._delete = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_list_clusters_with_search(self, client_with_mocks):
        """Test list_clusters with search parameter."""
        client_with_mocks._get.return_value = {'items': [], 'total': 0}
        await client_with_mocks.list_clusters(search="name like 'prod%'")
        client_with_mocks._get.assert_called_once_with(
            '/clusters',
            params={'page': 1, 'size': 100, 'search': "name like 'prod%'"},
        )

    @pytest.mark.asyncio
    async def test_list_clusters_without_search(self, client_with_mocks):
        """Test list_clusters without search parameter."""
        client_with_mocks._get.return_value = {'items': [], 'total': 0}
        await client_with_mocks.list_clusters()
        client_with_mocks._get.assert_called_once_with(
            '/clusters',
            params={'page': 1, 'size': 100},
        )

    @pytest.mark.asyncio
    async def test_get_cluster(self, client_with_mocks):
        """Test get_cluster calls correct path."""
        client_with_mocks._get.return_value = {'id': 'c1'}
        result = await client_with_mocks.get_cluster('c1')
        client_with_mocks._get.assert_called_once_with('/clusters/c1')
        assert result == {'id': 'c1'}

    @pytest.mark.asyncio
    async def test_create_cluster(self, client_with_mocks):
        """Test create_cluster posts body to correct path."""
        body = {'name': 'new-cluster'}
        client_with_mocks._post.return_value = {'id': 'new-id'}
        result = await client_with_mocks.create_cluster(body)
        client_with_mocks._post.assert_called_once_with('/clusters', body)
        assert result == {'id': 'new-id'}

    @pytest.mark.asyncio
    async def test_delete_cluster(self, client_with_mocks):
        """Test delete_cluster calls correct path with deprovision param."""
        client_with_mocks._delete.return_value = 204
        result = await client_with_mocks.delete_cluster('c1', deprovision=True)
        client_with_mocks._delete.assert_called_once_with(
            '/clusters/c1',
            params={'deprovision': 'true'},
        )
        assert result == 204



class TestOCMClientAPIWrappers:
    """Test OCM client API wrapper methods with mocked HTTP."""

    @pytest.fixture
    def ocm_client(self):
        with patch.dict('os.environ', {'OCM_TOKEN': 'offline-token', 'OCM_CLIENT_ID': ''}):
            with patch('awslabs.rosa_mcp_server.ocm_client.OCMClient._load_ocm_config', return_value={}):
                client = OCMClient(offline_token='test-token')
                client._access_token = 'valid-token'
                client._token_expiry = time.time() + 3600
                return client

    @pytest.mark.asyncio
    async def test_create_cluster(self, ocm_client):
        """Test create_cluster calls _post."""
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'new-cluster'}) as mock_post:
            result = await ocm_client.create_cluster({'name': 'test'})
            assert result == {'id': 'new-cluster'}
            mock_post.assert_called_once_with('/clusters', {'name': 'test'})

    @pytest.mark.asyncio
    async def test_update_cluster(self, ocm_client):
        with patch.object(ocm_client, '_patch', new_callable=AsyncMock, return_value={'id': 'c1'}) as mock_patch:
            result = await ocm_client.update_cluster('c1', {'name': 'updated'})
            assert result == {'id': 'c1'}
            mock_patch.assert_called_once_with('/clusters/c1', {'name': 'updated'})

    @pytest.mark.asyncio
    async def test_delete_cluster(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204) as mock_del:
            result = await ocm_client.delete_cluster('c1', deprovision=True)
            assert result == 204

    @pytest.mark.asyncio
    async def test_get_cluster_credentials(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'kubeconfig': 'data'}) as mock_get:
            result = await ocm_client.get_cluster_credentials('c1')
            assert result == {'kubeconfig': 'data'}

    @pytest.mark.asyncio
    async def test_get_install_logs(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'content': 'logs'}) as mock_get:
            result = await ocm_client.get_install_logs('c1', tail=50)
            assert result == {'content': 'logs'}

    @pytest.mark.asyncio
    async def test_get_uninstall_logs(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'content': 'logs'}) as mock_get:
            result = await ocm_client.get_uninstall_logs('c1')
            assert result == {'content': 'logs'}

    @pytest.mark.asyncio
    async def test_list_versions(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as mock_get:
            result = await ocm_client.list_versions(search="rosa_enabled = 'true'")
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_list_machine_pools(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as mock_get:
            result = await ocm_client.list_machine_pools('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_machine_pool(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'mp1'}) as mock_post:
            result = await ocm_client.create_machine_pool('c1', {'replicas': 3})
            assert result == {'id': 'mp1'}

    @pytest.mark.asyncio
    async def test_update_machine_pool(self, ocm_client):
        with patch.object(ocm_client, '_patch', new_callable=AsyncMock, return_value={'id': 'mp1'}) as mock_patch:
            result = await ocm_client.update_machine_pool('c1', 'mp1', {'replicas': 5})
            assert result == {'id': 'mp1'}

    @pytest.mark.asyncio
    async def test_delete_machine_pool(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204) as mock_del:
            result = await ocm_client.delete_machine_pool('c1', 'mp1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_list_idps(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as mock_get:
            result = await ocm_client.list_identity_providers('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_idp(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'idp1'}) as mock_post:
            result = await ocm_client.create_identity_provider('c1', {'type': 'htpasswd'})
            assert result == {'id': 'idp1'}

    @pytest.mark.asyncio
    async def test_delete_idp(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204) as mock_del:
            result = await ocm_client.delete_identity_provider('c1', 'idp1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_list_ingresses(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as mock_get:
            result = await ocm_client.list_ingresses('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_ingress(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'ing1'}) as mock_post:
            result = await ocm_client.create_ingress('c1', {'listening': 'external'})
            assert result == {'id': 'ing1'}

    @pytest.mark.asyncio
    async def test_update_ingress(self, ocm_client):
        with patch.object(ocm_client, '_patch', new_callable=AsyncMock, return_value={'id': 'ing1'}) as mock_patch:
            result = await ocm_client.update_ingress('c1', 'ing1', {'listening': 'internal'})
            assert result == {'id': 'ing1'}

    @pytest.mark.asyncio
    async def test_delete_ingress(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204) as mock_del:
            result = await ocm_client.delete_ingress('c1', 'ing1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_list_upgrades(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as mock_get:
            result = await ocm_client.list_upgrade_policies('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_upgrade(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'up1'}) as mock_post:
            result = await ocm_client.create_upgrade_policy('c1', {'version': '4.20.29'})
            assert result == {'id': 'up1'}

    @pytest.mark.asyncio
    async def test_list_node_pools(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as mock_get:
            result = await ocm_client.list_node_pools('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_node_pool(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'np1'}) as mock_post:
            result = await ocm_client.create_node_pool('c1', {'replicas': 2})
            assert result == {'id': 'np1'}

    @pytest.mark.asyncio
    async def test_update_node_pool(self, ocm_client):
        with patch.object(ocm_client, '_patch', new_callable=AsyncMock, return_value={'id': 'np1'}) as mock_patch:
            result = await ocm_client.update_node_pool('c1', 'np1', {'replicas': 4})
            assert result == {'id': 'np1'}

    @pytest.mark.asyncio
    async def test_delete_node_pool(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204) as mock_del:
            result = await ocm_client.delete_node_pool('c1', 'np1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_list_addons(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as mock_get:
            result = await ocm_client.list_cluster_addons('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_list_groups(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as mock_get:
            result = await ocm_client.list_groups('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_list_group_users(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as mock_get:
            result = await ocm_client.list_group_users('c1', 'dedicated-admins')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_add_group_user(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'u1'}) as mock_post:
            result = await ocm_client.add_user_to_group('c1', 'dedicated-admins', {'id': 'user1'})
            assert result == {'id': 'u1'}

    @pytest.mark.asyncio
    async def test_delete_group_user(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204) as mock_del:
            result = await ocm_client.remove_user_from_group('c1', 'dedicated-admins', 'user1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_get_autoscaler(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'max_nodes': 10}) as mock_get:
            result = await ocm_client.get_autoscaler('c1')
            assert result == {'max_nodes': 10}

    @pytest.mark.asyncio
    async def test_create_autoscaler(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'as1'}) as mock_post:
            result = await ocm_client.create_autoscaler('c1', {'max_nodes': 10})
            assert result == {'id': 'as1'}

    @pytest.mark.asyncio
    async def test_update_autoscaler(self, ocm_client):
        with patch.object(ocm_client, '_patch', new_callable=AsyncMock, return_value={'max_nodes': 20}) as mock_patch:
            result = await ocm_client.update_autoscaler('c1', {'max_nodes': 20})
            assert result == {'max_nodes': 20}

    @pytest.mark.asyncio
    async def test_delete_autoscaler(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204) as mock_del:
            result = await ocm_client.delete_autoscaler('c1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_close(self, ocm_client):
        ocm_client._http = MagicMock()
        ocm_client._http.aclose = AsyncMock()
        await ocm_client.close()
        assert ocm_client._http is None



    @pytest.mark.asyncio
    async def test_install_addon(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'a1'}) as m:
            result = await ocm_client.install_addon('c1', {'id': 'addon1'})
            assert result == {'id': 'a1'}

    @pytest.mark.asyncio
    async def test_get_addon_installation(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'id': 'a1'}) as m:
            result = await ocm_client.get_addon_installation('c1', 'addon1')
            assert result == {'id': 'a1'}

    @pytest.mark.asyncio
    async def test_uninstall_addon(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204) as m:
            result = await ocm_client.uninstall_addon('c1', 'addon1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_list_available_addons(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as m:
            result = await ocm_client.list_available_addons()
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_get_node_pool(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'id': 'np1'}) as m:
            result = await ocm_client.get_node_pool('c1', 'np1')
            assert result == {'id': 'np1'}

    @pytest.mark.asyncio
    async def test_get_machine_pool(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'id': 'mp1'}) as m:
            result = await ocm_client.get_machine_pool('c1', 'mp1')
            assert result == {'id': 'mp1'}

    @pytest.mark.asyncio
    async def test_list_tuning_configs(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as m:
            result = await ocm_client.list_tuning_configs('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_tuning_config(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 't1'}) as m:
            result = await ocm_client.create_tuning_config('c1', {'name': 'tc1'})
            assert result == {'id': 't1'}

    @pytest.mark.asyncio
    async def test_delete_tuning_config(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204) as m:
            result = await ocm_client.delete_tuning_config('c1', 't1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_get_kubelet_config(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'podPidsLimit': 4096}) as m:
            result = await ocm_client.get_kubelet_config('c1')
            assert result == {'podPidsLimit': 4096}

    @pytest.mark.asyncio
    async def test_update_kubelet_config(self, ocm_client):
        with patch.object(ocm_client, '_patch', new_callable=AsyncMock, return_value={'podPidsLimit': 8192}) as m:
            result = await ocm_client.update_kubelet_config('c1', {'podPidsLimit': 8192})
            assert result == {'podPidsLimit': 8192}

    @pytest.mark.asyncio
    async def test_get_vpc_config(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'vpc_id': 'vpc-123'}) as m:
            result = await ocm_client.get_cluster('c1')
            assert result == {'vpc_id': 'vpc-123'}

    @pytest.mark.asyncio
    async def test_list_break_glass_credentials(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}) as m:
            result = await ocm_client.list_break_glass_credentials('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_break_glass_credential(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'bg1'}) as m:
            result = await ocm_client.create_break_glass_credential('c1', {'ttl': '24h'})
            assert result == {'id': 'bg1'}



    @pytest.mark.asyncio
    async def test_get_break_glass_credential(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'id': 'bg1'}):
            result = await ocm_client.get_break_glass_credential('c1', 'bg1')
            assert result == {'id': 'bg1'}

    @pytest.mark.asyncio
    async def test_get_tuning_config(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'id': 't1'}):
            result = await ocm_client.get_tuning_config('c1', 't1')
            assert result == {'id': 't1'}

    @pytest.mark.asyncio
    async def test_update_tuning_config(self, ocm_client):
        with patch.object(ocm_client, '_patch', new_callable=AsyncMock, return_value={'id': 't1'}):
            result = await ocm_client.update_tuning_config('c1', 't1', {'spec': {}})
            assert result == {'id': 't1'}

    @pytest.mark.asyncio
    async def test_delete_kubelet_config(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204):
            result = await ocm_client.delete_kubelet_config('c1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_list_external_auths(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.list_external_auths('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_external_auth(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'ea1'}):
            result = await ocm_client.create_external_auth('c1', {'issuer': 'x'})
            assert result == {'id': 'ea1'}

    @pytest.mark.asyncio
    async def test_delete_external_auth(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204):
            result = await ocm_client.delete_external_auth('c1', 'ea1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_list_dns_domains(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.list_dns_domains()
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_dns_domain(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'd1'}):
            result = await ocm_client.create_dns_domain({'domain': 'x.com'})
            assert result == {'id': 'd1'}

    @pytest.mark.asyncio
    async def test_delete_dns_domain(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204):
            result = await ocm_client.delete_dns_domain('d1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_list_oidc_configs(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.list_oidc_configs()
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_oidc_config(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'oidc1'}):
            result = await ocm_client.create_oidc_config({'managed': True})
            assert result == {'id': 'oidc1'}

    @pytest.mark.asyncio
    async def test_delete_oidc_config(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204):
            result = await ocm_client.delete_oidc_config('oidc1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_list_machine_types(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.list_machine_types()
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_get_delete_protection(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'enabled': True}):
            result = await ocm_client.get_delete_protection('c1')
            assert result == {'enabled': True}

    @pytest.mark.asyncio
    async def test_update_delete_protection(self, ocm_client):
        with patch.object(ocm_client, '_patch', new_callable=AsyncMock, return_value={'enabled': False}):
            result = await ocm_client.update_delete_protection('c1', {'enabled': False})
            assert result == {'enabled': False}

    @pytest.mark.asyncio
    async def test_list_log_forwarders(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.list_log_forwarders('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_log_forwarder(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'lf1'}):
            result = await ocm_client.create_log_forwarder('c1', {'type': 'cloudwatch'})
            assert result == {'id': 'lf1'}

    @pytest.mark.asyncio
    async def test_delete_log_forwarder(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204):
            result = await ocm_client.delete_log_forwarder('c1', 'lf1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_list_sts_operator_roles(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.list_sts_operator_roles('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_list_sts_credential_requests(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.list_sts_credential_requests()
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_list_sts_policies(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.list_sts_policies()
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_get_cluster_operators(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.get_cluster_operators('c1')
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_validate_credentials(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'valid': True}):
            result = await ocm_client.validate_credentials({'aws': {}})
            assert result == {'valid': True}



    @pytest.mark.asyncio
    async def test_create_network_verification(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'nv1'}):
            result = await ocm_client.create_network_verification({'subnet_ids': []})
            assert result == {'id': 'nv1'}

    @pytest.mark.asyncio
    async def test_list_network_verifications(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.list_network_verifications()
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_get_log_forwarder(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'id': 'lf1'}):
            result = await ocm_client.get_log_forwarder('c1', 'lf1')
            assert result == {'id': 'lf1'}

    @pytest.mark.asyncio
    async def test_get_available_regions(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.get_available_regions({'aws': {}})
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_get_available_machine_types(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.get_available_machine_types({'aws': {}})
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_create_kubelet_config(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'podPidsLimit': 4096}):
            result = await ocm_client.create_kubelet_config('c1', {'podPidsLimit': 4096})
            assert result == {'podPidsLimit': 4096}

    @pytest.mark.asyncio
    async def test_list_cluster_addons(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.list_cluster_addons('c1')
            assert result == {'items': []}



    @pytest.mark.asyncio
    async def test_remove_user_from_group(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204):
            result = await ocm_client.remove_user_from_group('c1', 'group1', 'user1')
            assert result == 204

    @pytest.mark.asyncio
    async def test_get_uninstall_logs_with_tail(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'content': 'x'}):
            result = await ocm_client.get_uninstall_logs('c1', tail=100)
            assert result == {'content': 'x'}

    @pytest.mark.asyncio
    async def test_list_clusters(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'items': []}):
            result = await ocm_client.list_clusters(search="state = 'ready'")
            assert result == {'items': []}

    @pytest.mark.asyncio
    async def test_get_install_logs_no_tail(self, ocm_client):
        with patch.object(ocm_client, '_get', new_callable=AsyncMock, return_value={'content': 'logs'}):
            result = await ocm_client.get_install_logs('c1')
            assert result == {'content': 'logs'}

    @pytest.mark.asyncio
    async def test_delete_machine_pool_full(self, ocm_client):
        with patch.object(ocm_client, '_delete', new_callable=AsyncMock, return_value=204):
            result = await ocm_client.delete_machine_pool('c1', 'workers')
            assert result == 204

    @pytest.mark.asyncio
    async def test_create_upgrade_policy(self, ocm_client):
        with patch.object(ocm_client, '_post', new_callable=AsyncMock, return_value={'id': 'up1'}):
            result = await ocm_client.create_upgrade_policy('c1', {'version': '4.20'})
            assert result == {'id': 'up1'}




class TestOCMClientConfigLoading:
    """Test OCM config file loading."""

    def test_loads_config_from_file(self, tmp_path):
        config_dir = tmp_path / '.config' / 'ocm'
        config_dir.mkdir(parents=True)
        config_file = config_dir / 'ocm.json'
        config_file.write_text('{"refresh_token": "file-token", "client_id": "file-client"}')

        with patch('pathlib.Path.home', return_value=tmp_path):
            client = OCMClient(offline_token='env-token')
            # The env-token takes precedence but config loading was exercised
            assert client._offline_token == 'env-token'

    def test_returns_empty_when_no_config(self, tmp_path):
        with patch('pathlib.Path.home', return_value=tmp_path):
            client = OCMClient(offline_token='my-token')
            assert client._offline_token == 'my-token'

    def test_handles_invalid_json(self, tmp_path):
        config_dir = tmp_path / '.config' / 'ocm'
        config_dir.mkdir(parents=True)
        (config_dir / 'ocm.json').write_text('not-valid-json{{{')

        with patch('pathlib.Path.home', return_value=tmp_path):
            client = OCMClient(offline_token='my-token')
            assert client._offline_token == 'my-token'
