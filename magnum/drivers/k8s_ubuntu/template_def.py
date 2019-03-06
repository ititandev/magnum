import os

import netaddr
from oslo_log import log as logging
from oslo_utils import strutils

from magnum.common import clients
from magnum.conductor.handlers.common import cert_manager
import magnum.conf
from magnum.drivers.heat import template_def

CONF = magnum.conf.CONF
LOG = logging.getLogger(__name__)


class ServerAddressOutputMapping(template_def.NodeGroupOutputMapping):
    public_ip_output_key = None
    private_ip_output_key = None

    def __init__(self, dummy_arg, nodegroup_attr=None, nodegroup_uuid=None):
        self.nodegroup_attr = nodegroup_attr
        self.nodegroup_uuid = nodegroup_uuid
        self.heat_output = self.public_ip_output_key
        self.is_stack_param = False

    def set_output(self, stack, cluster_template, cluster):
        if not cluster_template.floating_ip_enabled:
            self.heat_output = self.private_ip_output_key

        LOG.debug("Using heat_output: %s", self.heat_output)
        super(ServerAddressOutputMapping,
              self).set_output(stack, cluster_template, cluster)


class NodeAddressOutputMapping(ServerAddressOutputMapping):
    public_ip_output_key = 'kube_minions'
    private_ip_output_key = 'kube_minions_private'


class UbuntuK8sTemplateDefinition(template_def.TemplateDefinition):
    """Kubernetes template for Ubuntu workers."""

    def __init__(self, *args, **kwargs):
        super(UbuntuK8sTemplateDefinition, self).__init__()

        self.bootstrap_token = kwargs.get("bootstrap_token")
        self.apiserver_address = kwargs.get("apiserver_address")
        self.public_network_id = kwargs.get("public_network_id")

        self.add_parameter('fixed_network',
                           cluster_attr='fixed_network')
        self.add_parameter('fixed_subnet',
                           cluster_attr='fixed_subnet')
        self.add_parameter('server_image',
                           cluster_template_attr='image_id')
        self.add_parameter('floating_ip_enabled',
                           cluster_template_attr='floating_ip_enabled',
                           param_type=bool)
        self.add_parameter('ssh_key_name',
                           cluster_attr='keypair')
        self.add_parameter('cluster_id',
                           cluster_attr='uuid',
                           param_type=str)

    def add_nodegroup_params(self, cluster):
        worker_ng = cluster.default_ng_worker
        self.add_parameter('number_of_workers',
                           nodegroup_attr='node_count',
                           nodegroup_uuid=worker_ng.uuid,
                           param_class=template_def.NodeGroupParameterMapping)
        self.add_parameter('server_flavor',
                           nodegroup_attr='flavor_id',
                           nodegroup_uuid=worker_ng.uuid,
                           param_class=template_def.NodeGroupParameterMapping)

        super(UbuntuK8sTemplateDefinition, self).add_nodegroup_params(cluster)

    def update_outputs(self, stack, cluster_template, cluster):
        worker_ng = cluster.default_ng_worker

        self.add_output('kube_minions',
                        nodegroup_attr='node_addresses',
                        nodegroup_uuid=worker_ng.uuid,
                        mapping_type=NodeAddressOutputMapping)

        super(UbuntuK8sTemplateDefinition, self).update_outputs(
            stack, cluster_template, cluster)

    @property
    def driver_module_path(self):
        return __name__[:__name__.rindex('.')]

    @property
    def template_path(self):
        return os.path.join(os.path.dirname(os.path.realpath(__file__)),
                            'heat_templates/kubeworkers.yaml')

    def get_env_files(self, cluster_template, cluster):
        return []

    def get_params(self, context, cluster_template, cluster, **kwargs):
        # Add all the params from the cluster's nodegroups
        self.add_nodegroup_params(cluster)

        extra_params = kwargs.pop('extra_params', {})

        extra_params["bootstrap_token"] = self.bootstrap_token
        extra_params["apiserver_address"] = self.apiserver_address
        extra_params['kubernetes_version'] = cluster.labels.get(
            'kube_tag', 'v1.13.2').lstrip("v")
        extra_params['external_network'] = self.public_network_id

        cluser_service_ip_range = cluster.labels.get(
            'service_cluster_ip_range', '10.96.0.0/12')
        service_ip_net = netaddr.IPNetwork(cluser_service_ip_range)
        extra_params['cluster_dns_service_ip'] = service_ip_net[10]

        # Certs
        ca_cert = cert_manager.get_cluster_ca_certificate(
            cluster,
            context=context
        )
        ca_cert_content = ca_cert.get_certificate()
        ca_key_content = ca_cert.get_decrypted_private_key()
        extra_params['ca_content'] = ca_cert_content
        extra_params['cakey_content'] = ca_key_content

        # CNI
        if cluster_template.network_driver == 'flannel':
            extra_params["pods_network_cidr"] = cluster.labels.get(
                'flannel_network_cidr', '10.100.0.0/16')
        if cluster_template.network_driver == 'calico':
            extra_params["pods_network_cidr"] = cluster.labels.get(
                'calico_ipv4pool', '192.168.0.0/16')

        # Keystone related
        osc = clients.OpenStackClients(context)
        extra_params['trustee_user_id'] = cluster.trustee_user_id
        extra_params['trustee_password'] = cluster.trustee_password
        if CONF.trust.cluster_user_trust:
            extra_params['trust_id'] = cluster.trust_id
        else:
            extra_params['trust_id'] = ""
        svc_kwargs = {
            'service_type': 'identity',
            'interface': CONF.trust.trustee_keystone_interface,
            'version': 3
        }
        if CONF.trust.trustee_keystone_region_name:
            svc_kwargs['region_name'] = CONF.trust.trustee_keystone_region_name
        extra_params['auth_url'] = osc.url_for(**svc_kwargs).rstrip('/')

        # Labels
        extra_params['cloud_provider_enabled'] = strutils.bool_from_string(
            cluster.labels.get("cloud_provider_enabled", "true")
        )

        return super(UbuntuK8sTemplateDefinition, self).get_params(
            context, cluster_template, cluster, extra_params=extra_params,
            **kwargs
        )
