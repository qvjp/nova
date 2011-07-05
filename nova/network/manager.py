# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
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

"""Network Hosts are responsible for allocating ips and setting up network.

There are multiple backend drivers that handle specific types of networking
topologies.  All of the network commands are issued to a subclass of
:class:`NetworkManager`.

**Related Flags**

:network_driver:  Driver to use for network creation
:flat_network_bridge:  Bridge device for simple network instances
:flat_interface:  FlatDhcp will bridge into this interface if set
:flat_network_dns:  Dns for simple network
:flat_network_dhcp_start:  Dhcp start for FlatDhcp
:vlan_start:  First VLAN for private networks
:vpn_ip:  Public IP for the cloudpipe VPN servers
:vpn_start:  First Vpn port for private networks
:cnt_vpn_clients:  Number of addresses reserved for vpn clients
:network_size:  Number of addresses in each private subnet
:floating_range:  Floating IP address block
:fixed_range:  Fixed IP address block
:date_dhcp_on_disassociate:  Whether to update dhcp when fixed_ip
                             is disassociated
:fixed_ip_disassociate_timeout:  Seconds after which a deallocated ip
                                 is disassociated
:create_unique_mac_address_attempts:  Number of times to attempt creating
                                      a unique mac address

"""

import datetime
import math
import netaddr
import socket
from eventlet import greenpool

from nova import context
from nova import db
from nova import exception
from nova import flags
from nova import ipv6
from nova import log as logging
from nova import manager
from nova import quota
from nova import utils
from nova import rpc
from nova.network import api as network_api
import random


LOG = logging.getLogger("nova.network.manager")


FLAGS = flags.FLAGS
flags.DEFINE_string('flat_network_bridge', 'br100',
                    'Bridge for simple network instances')
flags.DEFINE_string('flat_network_dns', '8.8.4.4',
                    'Dns for simple network')
flags.DEFINE_bool('flat_injected', True,
                  'Whether to attempt to inject network setup into guest')
flags.DEFINE_string('flat_interface', None,
                    'FlatDhcp will bridge into this interface if set')
flags.DEFINE_string('flat_network_dhcp_start', '10.0.0.2',
                    'Dhcp start for FlatDhcp')
flags.DEFINE_integer('vlan_start', 100, 'First VLAN for private networks')
flags.DEFINE_string('vlan_interface', None,
                    'vlans will bridge into this interface if set')
flags.DEFINE_integer('num_networks', 1, 'Number of networks to support')
flags.DEFINE_string('vpn_ip', '$my_ip',
                    'Public IP for the cloudpipe VPN servers')
flags.DEFINE_integer('vpn_start', 1000, 'First Vpn port for private networks')
flags.DEFINE_integer('network_size', 256,
                        'Number of addresses in each private subnet')
flags.DEFINE_string('floating_range', '4.4.4.0/24',
                    'Floating IP address block')
flags.DEFINE_string('fixed_range', '10.0.0.0/8', 'Fixed IP address block')
flags.DEFINE_string('fixed_range_v6', 'fd00::/48', 'Fixed IPv6 address block')
flags.DEFINE_string('gateway_v6', None, 'Default IPv6 gateway')
flags.DEFINE_integer('cnt_vpn_clients', 0,
                     'Number of addresses reserved for vpn clients')
flags.DEFINE_string('network_driver', 'nova.network.linux_net',
                    'Driver to use for network creation')
flags.DEFINE_bool('update_dhcp_on_disassociate', False,
                  'Whether to update dhcp when fixed_ip is disassociated')
flags.DEFINE_integer('fixed_ip_disassociate_timeout', 600,
                     'Seconds after which a deallocated ip is disassociated')
flags.DEFINE_integer('create_unique_mac_address_attempts', 5,
                     'Number of attempts to create unique mac address')

flags.DEFINE_bool('use_ipv6', False,
                  'use the ipv6')
flags.DEFINE_string('network_host', socket.gethostname(),
                    'Network host to use for ip allocation in flat modes')
flags.DEFINE_bool('fake_call', False,
                  'If True, skip using the queue and make local calls')


class AddressAlreadyAllocated(exception.Error):
    """Address was already allocated."""
    pass


class RPCAllocateFixedIP(object):
    """Mixin class originally for FlatDCHP and VLAN network managers.

    used since they share code to RPC.call allocate_fixed_ip on the
    correct network host to configure dnsmasq
    """
    def _allocate_fixed_ips(self, context, instance_id, host, networks):
        """Calls allocate_fixed_ip once for each network."""
        green_pool = greenpool.GreenPool()

        for network in networks:
            # NOTE(vish): if we are not multi_host pass to the network host
            if not network['multi_host']:
                host = network['host']
            if host != self.host:
                # need to call allocate_fixed_ip to correct network host
                topic = self.db.queue_get_for(context, FLAGS.network_topic, host)
                args = {}
                args['instance_id'] = instance_id
                args['network_id'] = network['id']

                green_pool.spawn_n(rpc.call, context, topic,
                                   {'method': '_rpc_allocate_fixed_ip',
                                    'args': args})
            else:
                # i am the correct host, run here
                self.allocate_fixed_ip(context, instance_id, network)

        # wait for all of the allocates (if any) to finish
        green_pool.waitall()

    def _rpc_allocate_fixed_ip(self, context, instance_id, network_id):
        """Sits in between _allocate_fixed_ips and allocate_fixed_ip to
        perform network lookup on the far side of rpc.
        """
        network = self.db.network_get(context, network_id)
        self.allocate_fixed_ip(context, instance_id, network)


class FloatingIP(object):
    """Mixin class for adding floating IP functionality to a manager."""
    def init_host_floating_ips(self):
        """Configures floating ips owned by host."""

        admin_context = context.get_admin_context()
        try:
            floating_ips = self.db.floating_ip_get_all_by_host(admin_context,
                                                               self.host)
        except exception.NotFound:
            return

        for floating_ip in floating_ips:
            if floating_ip.get('fixed_ip', None):
                fixed_address = floating_ip['fixed_ip']['address']
                # NOTE(vish): The False here is because we ignore the case
                #             that the ip is already bound.
                self.driver.bind_floating_ip(floating_ip['address'], False)
                self.driver.ensure_floating_forward(floating_ip['address'],
                                                    fixed_address)

    def allocate_for_instance(self, context, **kwargs):
        """Handles allocating the floating IP resources for an instance.

        calls super class allocate_for_instance() as well

        rpc.called by network_api
        """
        instance_id = kwargs.get('instance_id')
        project_id = kwargs.get('project_id')
        LOG.debug(_("floating IP allocation for instance |%s|"), instance_id,
                                                               context=context)
        # call the next inherited class's allocate_for_instance()
        # which is currently the NetworkManager version
        # do this first so fixed ip is already allocated
        ips = super(FloatingIP, self).allocate_for_instance(context, **kwargs)
        if hasattr(FLAGS, 'auto_assign_floating_ip'):
            # allocate a floating ip (public_ip is just the address string)
            public_ip = self.allocate_floating_ip(context, project_id)
            # set auto_assigned column to true for the floating ip
            self.db.floating_ip_set_auto_assigned(context, public_ip)
            # get the floating ip object from public_ip string
            floating_ip = self.db.floating_ip_get_by_address(context,
                                                             public_ip)

            # get the first fixed_ip belonging to the instance
            fixed_ips = self.db.fixed_ip_get_by_instance(context, instance_id)
            fixed_ip = fixed_ips[0] if fixed_ips else None

            # call to correct network host to associate the floating ip
            self.network_api.associate_floating_ip(context,
                                              floating_ip,
                                              fixed_ip,
                                              affect_auto_assigned=True)
        return ips

    def deallocate_for_instance(self, context, **kwargs):
        """Handles deallocating floating IP resources for an instance.

        calls super class deallocate_for_instance() as well.

        rpc.called by network_api
        """
        instance_id = kwargs.get('instance_id')
        LOG.debug(_("floating IP deallocation for instance |%s|"), instance_id,
                                                               context=context)

        fixed_ips = self.db.fixed_ip_get_by_instance(context, instance_id)
        # add to kwargs so we can pass to super to save a db lookup there
        kwargs['fixed_ips'] = fixed_ips
        for fixed_ip in fixed_ips:
            # disassociate floating ips related to fixed_ip
            for floating_ip in fixed_ip.floating_ips:
                address = floating_ip['address']
                self.network_api.disassociate_floating_ip(context, address)
                # deallocate if auto_assigned
                if floating_ip['auto_assigned']:
                    self.network_api.release_floating_ip(context,
                                                         address,
                                                         True)

        # call the next inherited class's deallocate_for_instance()
        # which is currently the NetworkManager version
        # call this after so floating IPs are handled first
        super(FloatingIP, self).deallocate_for_instance(context, **kwargs)

    def allocate_floating_ip(self, context, project_id):
        """Gets an floating ip from the pool."""
        # NOTE(tr3buchet): all networks hosts in zone now use the same pool
        LOG.debug("QUOTA: %s" % quota.allowed_floating_ips(context, 1))
        if quota.allowed_floating_ips(context, 1) < 1:
            LOG.warn(_('Quota exceeeded for %s, tried to allocate '
                       'address'),
                     context.project_id)
            raise quota.QuotaError(_('Address quota exceeded. You cannot '
                                     'allocate any more addresses'))
        # TODO(vish): add floating ips through manage command
        return self.db.floating_ip_allocate_address(context,
                                                    project_id)

    def associate_floating_ip(self, context, floating_address, fixed_address):
        """Associates an floating ip to a fixed ip."""
        self.db.floating_ip_fixed_ip_associate(context,
                                               floating_address,
                                               fixed_address)
        self.driver.bind_floating_ip(floating_address)
        self.driver.ensure_floating_forward(floating_address, fixed_address)

    def disassociate_floating_ip(self, context, floating_address):
        """Disassociates a floating ip."""
        fixed_address = self.db.floating_ip_disassociate(context,
                                                         floating_address)
        self.driver.unbind_floating_ip(floating_address)
        self.driver.remove_floating_forward(floating_address, fixed_address)

    def deallocate_floating_ip(self, context, floating_address):
        """Returns an floating ip to the pool."""
        self.db.floating_ip_deallocate(context, floating_address)


class NetworkManager(manager.SchedulerDependentManager):
    """Implements common network manager functionality.

    This class must be subclassed to support specific topologies.

    host management:
        hosts configure themselves for networks they are assigned to in the
        table upon startup. If there are networks in the table which do not
        have hosts, those will be filled in and have hosts configured
        as the hosts pick them up one at time during their periodic task.
        The one at a time part is to flatten the layout to help scale
    """

    timeout_fixed_ips = True

    def __init__(self, network_driver=None, *args, **kwargs):
        if not network_driver:
            network_driver = FLAGS.network_driver
        self.driver = utils.import_object(network_driver)
        self.network_api = network_api.API()
        super(NetworkManager, self).__init__(service_name='network',
                                                *args, **kwargs)

    def _update_dchp(self, context, network_ref):
        """Sets the listen address before sending update to the driver."""
        network_ref['dhcp_listen'] = self._get_dhcp_ip()
        return self.driver.update_dhcp(context, network_ref)

    def _get_dhcp_ip(self, context, network_ref):
        """Get the proper dhcp address to listen on.

        If it is a multi_host network, get the ip assigned to this host,
        otherwise, assume that dhcp is listening on the gateway."""
        if network_ref['multi_host']:
            return self.db.network_get_host_ip(context, FLAGS.host)
        else:
            return network_ref['gateway']

    def init_host(self):
        """Do any initialization that needs to be run if this is a
        standalone service.
        """
        # Set up this host for networks in which it's already
        # the designated network host.
        ctxt = context.get_admin_context()
        for network in self.db.network_get_all_by_host(ctxt, self.host):
            self._on_set_network_host(ctxt, network['id'])

    def periodic_tasks(self, context=None):
        """Tasks to be run at a periodic interval."""
        super(NetworkManager, self).periodic_tasks(context)
        if self.timeout_fixed_ips:
            now = utils.utcnow()
            timeout = FLAGS.fixed_ip_disassociate_timeout
            time = now - datetime.timedelta(seconds=timeout)
            num = self.db.fixed_ip_disassociate_all_by_timeout(context,
                                                               self.host,
                                                               time)
            if num:
                LOG.debug(_('Dissassociated %s stale fixed ip(s)'), num)

        # setup any new networks which have been created
        self.set_network_hosts(context)

    def set_network_host(self, context, network_id):
        """Safely sets the host of the network."""
        LOG.debug(_('setting network host'), context=context)
        host = self.db.network_set_host(context,
                                        network_id,
                                        self.host)
        if host == self.host:
            self._on_set_network_host(context, network_id)
        return host

    def set_network_hosts(self, context):
        """Set the network hosts for any networks which are unset."""
        networks = self.db.network_get_all(context)
        for network in networks:
            host = network['host']
            if not host:
                # return so worker will only grab 1 (to help scale flatter)
                return self.set_network_host(context, network['id'])

    def _get_networks_for_instance(self, context, instance_id, project_id):
        """Determine & return which networks an instance should connect to."""
        # TODO(tr3buchet) maybe this needs to be updated in the future if
        #                 there is a better way to determine which networks
        #                 a non-vlan instance should connect to
        networks = self.db.network_get_all(context)

        # return only networks which are not vlan networks and have host set
        return [network for network in networks if
                not network['vlan'] and network['host']]

    def allocate_for_instance(self, context, **kwargs):
        """Handles allocating the various network resources for an instance.

        rpc.called by network_api
        """
        instance_id = kwargs.pop('instance_id')
        host = kwargs.pop('host')
        project_id = kwargs.pop('project_id')
        type_id = kwargs.pop('instance_type_id')
        admin_context = context.elevated()
        LOG.debug(_("network allocations for instance %s"), instance_id,
                                                            context=context)
        networks = self._get_networks_for_instance(admin_context, instance_id,
                                                                  project_id)
        self._allocate_mac_addresses(context, instance_id, networks)
        self._allocate_fixed_ips(admin_context, instance_id, host, networks)
        return self.get_instance_nw_info(context, instance_id, type_id)

    def deallocate_for_instance(self, context, **kwargs):
        """Handles deallocating various network resources for an instance.

        rpc.called by network_api
        kwargs can contain fixed_ips to circumvent another db lookup
        """
        instance_id = kwargs.pop('instance_id')
        fixed_ips = kwargs.get('fixed_ips') or \
                  self.db.fixed_ip_get_by_instance(context, instance_id)
        LOG.debug(_("network deallocation for instance |%s|"), instance_id,
                                                               context=context)
        # deallocate fixed ips
        for fixed_ip in fixed_ips:
            self.deallocate_fixed_ip(context, fixed_ip['address'], **kwargs)

        # deallocate vifs (mac addresses)
        self.db.virtual_interface_delete_by_instance(context, instance_id)

    def get_instance_nw_info(self, context, instance_id, instance_type_id):
        """Creates network info list for instance.

        called by allocate_for_instance and netowrk_api
        context needs to be elevated
        :returns: network info list [(network,info),(network,info)...]
        where network = dict containing pertinent data from a network db object
        and info = dict containing pertinent networking data
        """
        # TODO(tr3buchet) should handle floating IPs as well?
        fixed_ips = self.db.fixed_ip_get_by_instance(context, instance_id)
        vifs = self.db.virtual_interface_get_by_instance(context, instance_id)
        flavor = self.db.instance_type_get_by_id(context,
                                                 instance_type_id)
        network_info = []
        # a vif has an address, instance_id, and network_id
        # it is also joined to the instance and network given by those IDs
        for vif in vifs:
            network = vif['network']

            # determine which of the instance's IPs belong to this network
            network_IPs = [fixed_ip['address'] for fixed_ip in fixed_ips if
                           fixed_ip['network_id'] == network['id']]

            # TODO(tr3buchet) eventually "enabled" should be determined
            def ip_dict(ip):
                return {
                    "ip": ip,
                    "netmask": network["netmask"],
                    "enabled": "1"}

            def ip6_dict():
                return {
                    "ip": ipv6.to_global(network['cidr_v6'],
                                         vif['address'],
                                         network['project_id']),
                    "netmask": network['netmask_v6'],
                    "enabled": "1"}
            network_dict = {
                'bridge': network['bridge'],
                'id': network['id'],
                'cidr': network['cidr'],
                'cidr_v6': network['cidr_v6'],
                'injected': network['injected']}
            info = {
                'label': network['label'],
                'gateway': network['gateway'],
                'broadcast': network['broadcast'],
                'mac': vif['address'],
                'rxtx_cap': flavor['rxtx_cap'],
                'dns': [network['dns']],
                'ips': [ip_dict(ip) for ip in network_IPs]}
            if network['cidr_v6']:
                info['ip6s'] = [ip6_dict()]
            # TODO(tr3buchet): handle ip6 routes here as well
            if network['gateway_v6']:
                info['gateway6'] = network['gateway_v6']
            network_info.append((network_dict, info))
        return network_info

    def _allocate_mac_addresses(self, context, instance_id, networks):
        """Generates mac addresses and creates vif rows in db for them."""
        for network in networks:
            vif = {'address': self.generate_mac_address(),
                   'instance_id': instance_id,
                   'network_id': network['id']}
            # try FLAG times to create a vif record with a unique mac_address
            for i in range(FLAGS.create_unique_mac_address_attempts):
                try:
                    self.db.virtual_interface_create(context, vif)
                    break
                except exception.VirtualInterfaceCreateException:
                    vif['address'] = self.generate_mac_address()
            else:
                self.db.virtual_interface_delete_by_instance(context,
                                                             instance_id)
                raise exception.VirtualInterfaceMacAddressException()

    def generate_mac_address(self):
        """Generate a mac address for a vif on an instance."""
        mac = [0x02, 0x16, 0x3e,
               random.randint(0x00, 0x7f),
               random.randint(0x00, 0xff),
               random.randint(0x00, 0xff)]
        return ':'.join(map(lambda x: "%02x" % x, mac))

    def add_fixed_ip_to_instance(self, context, instance_id, host, network_id):
        """Adds a fixed ip to an instance from specified network."""
        networks = [self.db.network_get(context, network_id)]
        self._allocate_fixed_ips(context, instance_id, host, networks)

    def allocate_fixed_ip(self, context, instance_id, network, **kwargs):
        """Gets a fixed ip from the pool."""
        # TODO(vish): when this is called by compute, we can associate compute
        #             with a network, or a cluster of computes with a network
        #             and use that network here with a method like
        #             network_get_by_compute_host
        address = self.db.fixed_ip_associate_pool(context.elevated(),
                                                  network['id'],
                                                  instance_id)
        vif = self.db.virtual_interface_get_by_instance_and_network(context,
                                                                instance_id,
                                                                network['id'])
        values = {'allocated': True,
                  'virtual_interface_id': vif['id']}
        self.db.fixed_ip_update(context, address, values)
        return address

    def deallocate_fixed_ip(self, context, address, **kwargs):
        """Returns a fixed ip to the pool."""
        self.db.fixed_ip_update(context, address,
                                {'allocated': False,
                                 'virtual_interface_id': None})

    def lease_fixed_ip(self, context, address):
        """Called by dhcp-bridge when ip is leased."""
        LOG.debug(_('Leased IP |%(address)s|'), locals(), context=context)
        fixed_ip = self.db.fixed_ip_get_by_address(context, address)
        instance = fixed_ip['instance']
        if not instance:
            raise exception.Error(_('IP %s leased that is not associated') %
                                  address)
        now = utils.utcnow()
        self.db.fixed_ip_update(context,
                                fixed_ip['address'],
                                {'leased': True,
                                 'updated_at': now})
        if not fixed_ip['allocated']:
            LOG.warn(_('IP |%s| leased that isn\'t allocated'), address,
                     context=context)

    def release_fixed_ip(self, context, address):
        """Called by dhcp-bridge when ip is released."""
        LOG.debug(_('Released IP |%(address)s|'), locals(), context=context)
        fixed_ip = self.db.fixed_ip_get_by_address(context, address)
        instance = fixed_ip['instance']
        if not instance:
            raise exception.Error(_('IP %s released that is not associated') %
                                  address)
        if not fixed_ip['leased']:
            LOG.warn(_('IP %s released that was not leased'), address,
                     context=context)
        self.db.fixed_ip_update(context,
                                fixed_ip['address'],
                                {'leased': False})
        if not fixed_ip['allocated']:
            self.db.fixed_ip_disassociate(context, address)
            # NOTE(vish): dhcp server isn't updated until next setup, this
            #             means there will stale entries in the conf file
            #             the code below will update the file if necessary
            if FLAGS.update_dhcp_on_disassociate:
                network_ref = self.db.fixed_ip_get_network(context, address)
                network_ref['dhcp_listen'] = self._get_dhcp_ip(context, network_ref)
                self._update_dhcp(context, network_ref)

    def create_networks(self, context, label, cidr, num_networks,
                        network_size, cidr_v6, gateway_v6, bridge,
                        bridge_interface, **kwargs):
        """Create networks based on parameters."""
        fixed_net = netaddr.IPNetwork(cidr)
        fixed_net_v6 = netaddr.IPNetwork(cidr_v6)
        significant_bits_v6 = 64
        network_size_v6 = 1 << 64
        for index in range(num_networks):
            start = index * network_size
            start_v6 = index * network_size_v6
            significant_bits = 32 - int(math.log(network_size, 2))
            cidr = '%s/%s' % (fixed_net[start], significant_bits)
            project_net = netaddr.IPNetwork(cidr)
            net = {}
            net['bridge'] = bridge
            net['bridge_interface'] = bridge_interface
            net['dns'] = FLAGS.flat_network_dns
            net['cidr'] = cidr
            net['netmask'] = str(project_net.netmask)
            net['gateway'] = str(project_net[1])
            net['broadcast'] = str(project_net.broadcast)
            net['dhcp_start'] = str(project_net[2])
            if num_networks > 1:
                net['label'] = '%s_%d' % (label, index)
            else:
                net['label'] = label

            if FLAGS.use_ipv6:
                cidr_v6 = '%s/%s' % (fixed_net_v6[start_v6],
                                     significant_bits_v6)
                net['cidr_v6'] = cidr_v6

                project_net_v6 = netaddr.IPNetwork(cidr_v6)

                if gateway_v6:
                    # use a pre-defined gateway if one is provided
                    net['gateway_v6'] = str(gateway_v6)
                else:
                    net['gateway_v6'] = str(project_net_v6[1])

                net['netmask_v6'] = str(project_net_v6._prefixlen)

            if kwargs.get('vpn', False):
                # this bit here is for vlan-manager
                del net['dns']
                vlan = kwargs['vlan_start'] + index
                net['vpn_private_address'] = str(project_net[2])
                net['dhcp_start'] = str(project_net[3])
                net['vlan'] = vlan
                net['bridge'] = 'br%s' % vlan

                # NOTE(vish): This makes ports unique accross the cloud, a more
                #             robust solution would be to make them uniq per ip
                net['vpn_public_port'] = kwargs['vpn_start'] + index

            # None if network with cidr or cidr_v6 already exists
            network = self.db.network_create_safe(context, net)

            if network:
                self._create_fixed_ips(context, network['id'])
            else:
                raise ValueError(_('Network with cidr %s already exists') %
                                   cidr)

    @property
    def _bottom_reserved_ips(self):  # pylint: disable=R0201
        """Number of reserved ips at the bottom of the range."""
        return 2  # network, gateway

    @property
    def _top_reserved_ips(self):  # pylint: disable=R0201
        """Number of reserved ips at the top of the range."""
        return 1  # broadcast

    def _create_fixed_ips(self, context, network_id):
        """Create all fixed ips for network."""
        network = self.db.network_get(context, network_id)
        # NOTE(vish): Should these be properties of the network as opposed
        #             to properties of the manager class?
        bottom_reserved = self._bottom_reserved_ips
        top_reserved = self._top_reserved_ips
        project_net = netaddr.IPNetwork(network['cidr'])
        num_ips = len(project_net)
        for index in range(num_ips):
            address = str(project_net[index])
            if index < bottom_reserved or num_ips - index < top_reserved:
                reserved = True
            else:
                reserved = False
            self.db.fixed_ip_create(context, {'network_id': network_id,
                                              'address': address,
                                              'reserved': reserved})

    def _allocate_fixed_ips(self, context, instance_id, host, networks):
        """Calls allocate_fixed_ip once for each network."""
        raise NotImplementedError()

    def _on_set_network_host(self, context, network_id):
        """Called when this host becomes the host for a network."""
        raise NotImplementedError()

    def setup_compute_network(self, context, instance_id):
        """Sets up matching network for compute hosts.

        this code is run on and by the compute host, not on network
        hosts
        """
        raise NotImplementedError()


class FlatManager(NetworkManager):
    """Basic network where no vlans are used.

    FlatManager does not do any bridge or vlan creation.  The user is
    responsible for setting up whatever bridge is specified in
    flat_network_bridge (br100 by default).  This bridge needs to be created
    on all compute hosts.

    The idea is to create a single network for the host with a command like:
    nova-manage network create 192.168.0.0/24 1 256. Creating multiple
    networks for for one manager is currently not supported, but could be
    added by modifying allocate_fixed_ip and get_network to get the a network
    with new logic instead of network_get_by_bridge. Arbitrary lists of
    addresses in a single network can be accomplished with manual db editing.

    If flat_injected is True, the compute host will attempt to inject network
    config into the guest.  It attempts to modify /etc/network/interfaces and
    currently only works on debian based systems. To support a wider range of
    OSes, some other method may need to be devised to let the guest know which
    ip it should be using so that it can configure itself. Perhaps an attached
    disk or serial device with configuration info.

    Metadata forwarding must be handled by the gateway, and since nova does
    not do any setup in this mode, it must be done manually.  Requests to
    169.254.169.254 port 80 will need to be forwarded to the api server.

    """

    timeout_fixed_ips = False

    def _allocate_fixed_ips(self, context, instance_id, host, networks):
        """Calls allocate_fixed_ip once for each network."""
        for network in networks:
            self.allocate_fixed_ip(context, instance_id, network)

    def deallocate_fixed_ip(self, context, address, **kwargs):
        """Returns a fixed ip to the pool."""
        super(FlatManager, self).deallocate_fixed_ip(context, address,
                                                              **kwargs)
        self.db.fixed_ip_disassociate(context, address)

    def setup_compute_network(self, context, instance_id):
        """Network is created manually.

        this code is run on and by the compute host, not on network hosts
        """
        pass

    def _on_set_network_host(self, context, network_id):
        """Called when this host becomes the host for a network."""
        net = {}
        net['injected'] = FLAGS.flat_injected
        net['dns'] = FLAGS.flat_network_dns
        self.db.network_update(context, network_id, net)


class FlatDHCPManager(FloatingIP, RPCAllocateFixedIP, NetworkManager):
    """Flat networking with dhcp.

    FlatDHCPManager will start up one dhcp server to give out addresses.
    It never injects network settings into the guest. It also manages bridges.
    Otherwise it behaves like FlatManager.

    """

    def init_host(self):
        """Do any initialization that needs to be run if this is a
        standalone service.
        """
        self.driver.init_host()
        self.driver.ensure_metadata_ip()

        super(FlatDHCPManager, self).init_host()
        self.init_host_floating_ips()

        self.driver.metadata_forward()

    def setup_compute_network(self, context, instance_id):
        """Sets up matching networks for compute hosts.

        this code is run on and by the compute host, not on network hosts
        """
        networks = db.network_get_all_by_instance(context, instance_id)
        for network in networks:
            if not network['multi_host']:
                self.driver.ensure_bridge(network['bridge'],
                                          network['bridge_interface'])

    def allocate_fixed_ip(self, context, instance_id, network):
        """Allocate flat_network fixed_ip, then setup dhcp for this network."""
        address = super(FlatDHCPManager, self).allocate_fixed_ip(context,
                                                                 instance_id,
                                                                 network)
        if not FLAGS.fake_network:
            self._update_dhcp(context, network)

    def _on_set_network_host(self, context, network_id):
        """Called when this host becomes the host for a project."""
        net = {}
        net['dhcp_start'] = FLAGS.flat_network_dhcp_start
        self.db.network_update(context, network_id, net)
        network = db.network_get(context, network_id)
        self.driver.ensure_bridge(network['bridge'],
                                  network['bridge_interface'],
                                  network)
        if not FLAGS.fake_network:
            network_ref = self.db.network_get(context, network_id)
            self._update_dhcp(context, network_ref)
            if(FLAGS.use_ipv6):
                self.driver.update_ra(context, network_ref)
                gateway = utils.get_my_linklocal(network_ref['bridge'])
                self.db.network_update(context, network_id,
                                       {'gateway_v6': gateway})


class VlanManager(RPCAllocateFixedIP, FloatingIP, NetworkManager):
    """Vlan network with dhcp.

    VlanManager is the most complicated.  It will create a host-managed
    vlan for each project.  Each project gets its own subnet.  The networks
    and associated subnets are created with nova-manage using a command like:
    nova-manage network create 10.0.0.0/8 3 16.  This will create 3 networks
    of 16 addresses from the beginning of the 10.0.0.0 range.

    A dhcp server is run for each subnet, so each project will have its own.
    For this mode to be useful, each project will need a vpn to access the
    instances in its subnet.

    """

    def init_host(self):
        """Do any initialization that needs to be run if this is a
        standalone service.
        """

        self.driver.init_host()
        self.driver.ensure_metadata_ip()

        NetworkManager.init_host(self)
        self.init_host_floating_ips()

        self.driver.metadata_forward()

    def allocate_fixed_ip(self, context, instance_id, network, **kwargs):
        """Gets a fixed ip from the pool."""
        if kwargs.get('vpn', None):
            address = network['vpn_private_address']
            self.db.fixed_ip_associate(context,
                                       address,
                                       instance_id)
        else:
            address = self.db.fixed_ip_associate_pool(context,
                                                      network['id'],
                                                      instance_id)
        vif = self.db.virtual_interface_get_by_instance_and_network(context,
                                                                 instance_id,
                                                                 network['id'])
        values = {'allocated': True,
                  'virtual_interface_id': vif['id']}
        self.db.fixed_ip_update(context, address, values)
        if not FLAGS.fake_network:
            self._update_dhcp(context, network)

    def add_network_to_project(self, context, project_id):
        """Force adds another network to a project."""
        self.db.network_associate(context, project_id, force=True)

    def setup_compute_network(self, context, instance_id):
        """Sets up matching network for compute hosts.
        this code is run on and by the compute host, not on network hosts
        """
        networks = self.db.network_get_all_by_instance(context, instance_id)
        for network in networks:
            if not network['multi_host']:
                self.driver.ensure_vlan_bridge(network['vlan'],
                                               network['bridge'],
                                               network['bridge_interface'])

    def _get_networks_for_instance(self, context, instance_id, project_id):
        """Determine which networks an instance should connect to."""
        # get networks associated with project
        networks = self.db.project_get_networks(context, project_id)

        # return only networks which have host set
        return [network for network in networks if network['host']]

    def create_networks(self, context, **kwargs):
        """Create networks based on parameters."""
        # Check that num_networks + vlan_start is not > 4094, fixes lp708025
        if kwargs['num_networks'] + kwargs['vlan_start'] > 4094:
            raise ValueError(_('The sum between the number of networks and'
                               ' the vlan start cannot be greater'
                               ' than 4094'))

        # check that num networks and network size fits in fixed_net
        fixed_net = netaddr.IPNetwork(kwargs['cidr'])
        if len(fixed_net) < kwargs['num_networks'] * kwargs['network_size']:
            raise ValueError(_('The network range is not big enough to fit '
                  '%(num_networks)s. Network size is %(network_size)s') %
                  kwargs)

        NetworkManager.create_networks(self, context, vpn=True, **kwargs)

    def _on_set_network_host(self, context, network_id):
        """Called when this host becomes the host for a network."""
        network = self.db.network_get(context, network_id)
        if not network['vpn_public_address']:
            net = {}
            address = FLAGS.vpn_ip
            net['vpn_public_address'] = address
            db.network_update(context, network_id, net)
        else:
            address = network['vpn_public_address']
        self.driver.ensure_vlan_bridge(network['vlan'],
                                       network['bridge'],
                                       network['bridge_interface'],
                                       network)

        # NOTE(vish): only ensure this forward if the address hasn't been set
        #             manually.
        if address == FLAGS.vpn_ip and hasattr(self.driver,
                                               "ensure_vlan_forward"):
            self.driver.ensure_vlan_forward(FLAGS.vpn_ip,
                                            network['vpn_public_port'],
                                            network['vpn_private_address'])
        if not FLAGS.fake_network:
            network_ref = self.db.network_get(context, network_id)
            self._update_dhcp(context, network_ref)
            if(FLAGS.use_ipv6):
                self.driver.update_ra(context, network_ref)
                gateway = utils.get_my_linklocal(network_ref['bridge'])
                self.db.network_update(context, network_id,
                                       {'gateway_v6': gateway})

    @property
    def _bottom_reserved_ips(self):
        """Number of reserved ips at the bottom of the range."""
        return super(VlanManager, self)._bottom_reserved_ips + 1  # vpn server

    @property
    def _top_reserved_ips(self):
        """Number of reserved ips at the top of the range."""
        parent_reserved = super(VlanManager, self)._top_reserved_ips
        return parent_reserved + FLAGS.cnt_vpn_clients
