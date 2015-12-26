#    Copyright 2015 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
import os
import urllib
import urlparse
import traceback

import bs4
from devops.helpers.helpers import wait
from proboscis.asserts import assert_equal
from proboscis.asserts import assert_not_equal
from proboscis.asserts import assert_true
from proboscis import asserts
from proboscis import test
import requests

from fuelweb_test.helpers import os_actions
from fuelweb_test.helpers import checkers
from fuelweb_test import logger
from fuelweb_test.helpers.decorators import log_snapshot_after_test
from fuelweb_test import settings as conf

from fuelweb_test.tests.base_test_case import SetupEnvironment
from fuelweb_test.tests.base_test_case import TestBasic


class ZabbixWeb(object):
    def __init__(self, public_vip, username, password, verify=False):
        self.session = requests.Session()
        self.base_url = "https://{0}/zabbix/".format(public_vip)
        self.username = username
        self.password = password
        self.verify = verify

    def login(self):
        login_params = urllib.urlencode({'request': '',
                                         'name': self.username,
                                         'password': self.password,
                                         'autologin': 1,
                                         'enter': 'Sign in'})
        url = urlparse.urljoin(self.base_url, '?{0}'.format(login_params))
        response = self.session.post(url, verify=self.verify)

        assert_equal(response.status_code, 200,
                     "Login to Zabbix failed: {0}".format(response.content))

    def get_trigger_statuses(self):
        url = urlparse.urljoin(self.base_url, 'tr_status.php')
        response = self.session.get(url, verify=self.verify)

        assert_equal(response.status_code, 200,
                     "Getting Zabbix trigger statuses failed: {0}"
                     .format(response.content))

        return response.content

    def get_screens(self):
        url = urlparse.urljoin(self.base_url, 'screens.php')
        response = self.session.get(url, verify=self.verify)

        assert_equal(response.status_code, 200,
                     "Getting Zabbix screens failed: {0}"
                     .format(response.content))

        return response.content


@test(groups=["plugins", "zabbix_plugins"])
class ZabbixAndLbaasPlugins(TestBasic):
    """ZabbixPlugin."""

    @classmethod
    def check_neutron_agents_statuses(cls, os_conn):
        agents_list = os_conn.list_agents()

        for a in agents_list['agents']:
            asserts.assert_equal(
                a['alive'], True,
                'Neutron agent {0} is not alive'. format(a['binary']))
            asserts.assert_true(
                a['admin_state_up'],
                "Admin state is down for agent {0}".format(a['binary']))

        lb_agent = [a for a in agents_list["agents"]
                    if a['binary'] == 'neutron-lbaas-agent']

        logger.debug("LbaaS agent list is {0}".format(lb_agent))

        asserts.assert_equal(
            len(lb_agent), 1,
            'There is not LbaaS agent in neutron agent list output')

    @classmethod
    def check_lbass_work(cls, os_conn):
        # create pool
        pool = os_conn.create_pool(pool_name='lbaas_pool')

        logger.debug('pull is {0}'.format(pool))

        # create vip
        vip = os_conn.create_vip(name='lbaas_vip',
                                 protocol='HTTP',
                                 port=80,
                                 pool=pool)

        logger.debug('vip is {0}'.format(vip))

        # get list of vips
        lb_vip_list = os_conn.get_vips()

        logger.debug(
            'Initial state of vip is {0}'.format(
                os_conn.get_vip(lb_vip_list['vips'][0]['id'])))

        # wait for active status
        try:
            wait(lambda: os_conn.get_vip(
                lb_vip_list['vips'][0]['id'])['vip']['status'] == 'ACTIVE',
                timeout=120 * 60)
        except:
            logger.error(traceback.format_exc())
            vip_state = os_conn.get_vip(
                lb_vip_list['vips'][0]['id'])['vip']['status']
            asserts.assert_equal(
                'ACTIVE', vip_state,
                "Vip is not active, current state is {0}".format(vip_state))
    def setup_zabbix_plugin(self,
                            cluster_id,
                            zabbix_username='admin',
                            zabbix_password='zabbix'):
        plugin_name = 'zabbix_monitoring'

        assert_true(
            self.fuel_web.check_plugin_exists(cluster_id, plugin_name),
            "Plugin couldn't be enabled. Check plugin version. Test aborted")
        plugin_options = {'metadata/enabled': True,
                          'username/value': zabbix_username,
                          'password/value': zabbix_password}
        self.fuel_web.update_plugin_data(
                cluster_id, plugin_name, plugin_options)
     
    def setup_lbaas_plugin(self, cluster_id):
        plugin_name = 'lbaas'

        assert_true(
            self.fuel_web.check_plugin_exists(cluster_id, plugin_name),
            "Plugin couldn't be enabled. Check plugin version. Test aborted")
        plugin_options = {'metadata/enabled': True}
        self.fuel_web.update_plugin_data(
            cluster_id, plugin_name, plugin_options)

    @test(depends_on=[SetupEnvironment.prepare_slaves_5],
          groups=["deploy_zabbix_lbaas_ha"])
    @log_snapshot_after_test
    def deploy_zabbix_lbaas_ha(self):
        """Deploy cluster in ha mode with zabbix plugin

        Scenario:
            1. Upload plugins to the master node
            2. Install plugins
            3. Create cluster
            4. Add 3 nodes with controller role
            5. Add 1 node with compute role
            6. Add 1 node with cinder role
            7. Deploy the cluster
            8. Check cluster network mode
            9. Run network verification
            10. Check neutron agents statuses
            11. Check if lbaas is functional
            12. Run OSTF
            13. Check zabbix service in pacemaker
            14. Check login to zabbix dashboard

        Duration 70m
        Snapshot deploy_zabbix_ha

        """
        self.env.revert_snapshot("ready_with_5_slaves")

        with self.env.d_env.get_admin_remote() as remote:
            checkers.upload_tarball(
                remote, conf.ZABBIX_PLUGIN_PATH, "/var")
            checkers.upload_tarball(
                remote, conf.LBAAS_PLUGIN_PATH, "/var")
            checkers.install_plugin_check_code(
                remote,
                plugin=os.path.basename(conf.ZABBIX_PLUGIN_PATH))
            checkers.install_plugin_check_code(
                remote,
                plugin=os.path.basename(conf.LBAAS_PLUGIN_PATH))


        cluster_id = self.fuel_web.create_cluster(
            name=self.__class__.__name__,
            mode=conf.DEPLOYMENT_MODE,
            settings={
                "net_provider": 'neutron',
                "net_segment_type": conf.NEUTRON_SEGMENT_TYPE,
            }
        )

        zabbix_username = 'admin'
        zabbix_password = 'zabbix'
        self.setup_zabbix_plugin(cluster_id, zabbix_username, zabbix_password)

        self.setup_lbaas_plugin(cluster_id)

        self.fuel_web.update_nodes(
            cluster_id,
            {
                "slave-01": ["controller"],
                "slave-02": ["controller"],
                "slave-03": ["controller"],
                "slave-04": ["compute"],
                "slave-05": ["cinder"]
            }
        )

        # Deploy cluster with 2 plugins
        self.fuel_web.deploy_cluster_wait(cluster_id) 

        # Check network provider
        cluster = self.fuel_web.client.get_cluster(cluster_id)
        asserts.assert_equal(str(cluster['net_provider']), 'neutron')

        # Verify networks
        self.fuel_web.verify_network(cluster_id)

        # Connect to controller and check lbaas
        #public_vip = self.fuel_web.get_public_vip(cluster_id)
        #controller = self.fuel_web.get_nailgun_node_by_name('slave-01')
        #os_conn = os_actions.OpenStackActions(public_vip)

        #self.check_neutron_agents_statuses(os_conn)

        #self.check_lbass_work(os_conn)

        # Run OSTF
        self.fuel_web.run_ostf(cluster_id=cluster_id)

        # Check Zabbix
        cmd = "crm resource status p_zabbix-server"
        with self.fuel_web.get_ssh_for_node("slave-01") as remote:
            response = remote.execute(cmd)["stdout"][0]
        assert_true("p_zabbix-server is running" in response,
                    "p_zabbix-server resource wasn't found in pacemaker:\n{0}"
                    .format(response))

        public_vip = self.fuel_web.get_public_vip(cluster_id)

        zabbix_web = ZabbixWeb(public_vip, zabbix_username, zabbix_password)
        zabbix_web.login()

        screens_html = bs4.BeautifulSoup(zabbix_web.get_screens())
        screens_links = screens_html.find_all('a')
        assert_true(any('charts.php?graphid=' in link.get('href')
                        for link in screens_links),
                    "Zabbix screen page does not contain graphs:\n{0}".
                    format(screens_links))

        self.env.make_snapshot("deploy_zabbix_lbaas_ha") 
