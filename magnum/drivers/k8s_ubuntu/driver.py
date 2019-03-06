import base64
import datetime
import functools
import os
import random
import string
import time
import yaml

import jinja2
import netaddr
from neutronclient.common import exceptions as neutron_exceptions
from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import fileutils
from oslo_utils import netutils
from oslo_utils import strutils
from oslo_utils import timeutils
from oslo_utils import uuidutils

from magnum.common import clients
from magnum.common import context as mag_ctx
from magnum.common import exception
from magnum.common import keystone
from magnum.common import neutron
from magnum.common import octavia
from magnum.conductor.handlers.common import cert_manager
from magnum.conductor.handlers.common import trust_manager
from magnum.conductor import utils as conductor_utils
from magnum.drivers.heat import driver
from magnum.drivers.k8s_ubuntu import template_def
from magnum.drivers.k8s_ubuntu.utils import kubectl
from magnum.objects import fields

LOG = logging.getLogger(__name__)
CONF = cfg.CONF
TEMPLATES_DIR = (os.path.dirname(os.path.realpath(__file__)) +
                 '/kube_templates/')


class Driver(driver.HeatDriver):
    def __init__(self, *kargs, **kwargs):
        template_loader = jinja2.FileSystemLoader(
            searchpath=os.path.dirname(TEMPLATES_DIR)
        )
        self.jinja_env = jinja2.Environment(
            loader=template_loader, autoescape=True, trim_blocks=True,
            lstrip_blocks=True
        )

        self.kubectl = kubectl.KubeCtl(
            bin="/usr/bin/kubectl",
            global_flags="--kubeconfig %s" % CONF.kubernetes.kubeconfig
        )

        # The token for kubelet tls bootstraping.
        self.bootstrap_token = None
        # The VIP address of kube-apiserver load balancer.
        self.apiserver_address = None

        self.public_network_id = None

    def get_template_definition(self):
        """Gets the template definition.

        Pass the params needed for Heat template.
        """
        return template_def.UbuntuK8sTemplateDefinition(
            bootstrap_token=self.bootstrap_token,
            apiserver_address=self.apiserver_address,
            public_network_id=self.public_network_id
        )

    @property
    def provides(self):
        return [
            {'server_type': 'container',
             'os': 'ubuntu',
             'coe': 'kubernetes'},
        ]

    def _apply_manifest(self, params, manifest_template, client=None):
        template = self.jinja_env.get_template(manifest_template)
        body = template.render(params)
        client = client or self.kubectl
        client.apply(definition=body)

    def _delete_manifest(self, params, manifest_template):
        template = self.jinja_env.get_template(manifest_template)
        body = template.render(params)
        self.kubectl.delete(definition=body)

    def _generate_random_string(self, length):
        return ''.join(random.SystemRandom().choice(
            string.ascii_lowercase + string.digits) for _ in range(length))

    def _wait_for_apiserver(self, cluster_id, cluster_kubectl, timeout=300):
        """Waits for apiserver up and running.

        1. All the pods of the cluster namespace in the seed cluster should be
           in Running status.
        2. Namespaces in the new cluster should be Active.
        """
        time.sleep(3)
        start_time = time.time()

        while True:
            ready_pods = 0
            pods = self.kubectl.get("pods", namespace=cluster_id)
            for pod in pods:
                if (pod.get("status", {}).get("phase") and
                        pod["status"]["phase"] == "Running"):
                    ready_pods += 1

            if len(pods) == ready_pods:
                try:
                    namespaces = cluster_kubectl.get("namespaces",
                                                     print_error=False)
                    if len(namespaces) != 3:
                        raise Exception('Cluster creation failed.')

                    ready_ns = 0
                    for ns in namespaces:
                        if (ns.get("status", {}).get("phase") and
                                ns["status"]["phase"] == "Active"):
                            ready_ns += 1

                    if ready_ns == 3:
                        break
                except Exception:
                    pass

            if (time.time() - timeout) > start_time:
                raise exception.ClusterCreationTimeout(cluster_uuid=cluster_id)

            time.sleep(1)

    def _get_kubeconfig(self, context, cluster, ca_cert_encoded=None):
        """Prepare kubeconfig file to interact with the new cluster."""

        # TODO(lxkong): This kubeconfig file should be stored in a shared place
        # between magnum controllers.
        cluster_cache_dir = os.path.join(
            CONF.cluster.temp_cache_dir, cluster.uuid
        )
        fileutils.ensure_tree(cluster_cache_dir)
        kubeconfig_path = os.path.join(cluster_cache_dir, "kubeconfig")

        if os.path.exists(kubeconfig_path):
            return kubeconfig_path

        if ca_cert_encoded is None:
            ca_cert = cert_manager.get_cluster_ca_certificate(
                cluster,
                context=context
            )
            ca_cert_encoded = base64.b64encode(ca_cert.get_certificate())

        admin_cert = cert_manager.get_cluster_magnum_cert(
            cluster,
            context=context
        )
        admin_cert_encoded = base64.b64encode(admin_cert.get_certificate())
        admin_key_encoded = base64.b64encode(
            admin_cert.get_decrypted_private_key())

        split_ret = netutils.urlsplit(cluster.api_address)
        external_apiserver_address = split_ret.netloc.split(":")[0]

        kubeconfig_params = {
            "cluster_id": cluster.uuid,
            "apiserver_address": external_apiserver_address,
            "ca_cert": ca_cert_encoded,
            "cert": admin_cert_encoded,
            "key": admin_key_encoded,
            "user": "admin",
        }

        kubeconfig_template = self.jinja_env.get_template("kubeconfig.j2")
        kubeconfig = kubeconfig_template.render(kubeconfig_params)
        with open(kubeconfig_path, 'w') as fd:
            fd.write(kubeconfig)

        return kubeconfig_path

    def _master_lb_fip_enabled(self, cluster, cluster_template):
        lb_fip_enabled = cluster.labels.get(
            "master_lb_floating_ip_enabled",
            cluster_template.floating_ip_enabled
        )

        return strutils.bool_from_string(lb_fip_enabled)

    def _create_vip_port(self, context, cluster, cluster_template):
        """Create port for kube-apiserver load balancer.

        This method should be called before creating apiserver, because:
        1. We need an IP address to generate kube-apiserver certificates.
        2. We need to specify the port to create Service for kube-apiserver.
        """
        ext_net_id = cluster_template.external_network_id or "public"
        if not uuidutils.is_uuid_like(ext_net_id):
            ext_net_id = neutron.get_network_id(context, ext_net_id)

        network_client = clients.OpenStackClients(context).neutron()
        vip_port = None
        fip = None
        port_info = {}

        try:
            body = {
                'port': {
                    'name': "magnum_%s_vip" % cluster.uuid,
                    'admin_state_up': True,
                }
            }
            if cluster_template.fixed_network:
                body["port"].update(
                    {"network_id": cluster_template.fixed_network}
                )
            if cluster_template.fixed_subnet:
                body['port'].update(
                    {
                        'fixed_ips': [
                            {'subnet_id': cluster_template.fixed_subnet}
                        ]
                    }
                )
            port = network_client.create_port(body)
            vip_port = port['port']
            LOG.info(
                "port %s created for cluster %s", vip_port["id"], cluster.uuid
            )

            port_info["port_id"] = vip_port["id"]
            port_info["private_ip"] = vip_port["fixed_ips"][0]["ip_address"]

            # NOTE: tags has length limit
            tag_info = {"magnum": cluster.uuid}
            tags_body = {"tags": [jsonutils.dumps(tag_info)]}
            network_client.replace_tag("ports", vip_port["id"], tags_body)

            if self._master_lb_fip_enabled(cluster, cluster_template):
                fip = network_client.create_floatingip(
                    body={
                        'floatingip': {
                            'floating_network_id': ext_net_id,
                            'port_id': vip_port["id"],
                            'description': ('Load balancer VIP for Magnum '
                                            'cluster %s' % cluster.uuid)
                        }
                    }
                )['floatingip']
                LOG.info(
                    "floating IP %s created for cluster %s",
                    fip["floating_ip_address"], cluster.uuid
                )

                port_info["public_ip"] = fip["floating_ip_address"]
        except neutron_exceptions.NeutronClientException as e:
            LOG.exception('Failed to create vip port for apiserver.')
            raise exception.NetworkResourceCreationFailed(
                cluster_uuid=cluster.uuid,
                msg=str(e)
            )

        return port_info

    def create_cluster(self, context, cluster, cluster_create_timeout):
        LOG.info("Starting to create cluster %s", cluster.uuid)

        cluster_template = conductor_utils.retrieve_cluster_template(
            context,
            cluster
        )

        cluser_service_ip_range = cluster.labels.get(
            'service_cluster_ip_range', '10.97.0.0/16'
        )
        if cluster_template.network_driver == 'flannel':
            cluser_pod_ip_range = cluster.labels.get(
                'flannel_network_cidr', '10.100.0.0/16'
            )
        if cluster_template.network_driver == 'calico':
            cluser_pod_ip_range = cluster.labels.get(
                'calico_ipv4pool', '192.168.0.0/16'
            )

        port_info = self._create_vip_port(context, cluster, cluster_template)

        # This address should be internal IP that other services could
        # communicate with.
        self.apiserver_address = port_info["private_ip"]
        external_apiserver_address = port_info.get("public_ip",
                                                   port_info["private_ip"])

        # The master address is always the private VIP address.
        cluster.api_address = 'https://%s:6443' % external_apiserver_address
        master_ng = cluster.default_ng_master
        setattr(master_ng, "node_addresses", [self.apiserver_address])
        master_ng.save()

        self.public_network_id = (
            cluster_template.external_network_id or "public")
        if not uuidutils.is_uuid_like(self.public_network_id):
            self.public_network_id = neutron.get_network_id(
                context,
                self.public_network_id
            )

        ca_cert = cert_manager.get_cluster_ca_certificate(
            cluster,
            context=context
        )
        ca_cert_encoded = base64.b64encode(ca_cert.get_certificate())
        ca_key_encoded = base64.b64encode(ca_cert.get_decrypted_private_key())

        cloud_provider_enabled = strutils.bool_from_string(
            cluster.labels.get("cloud_provider_enabled", "true")
        )

        params = {
            "namespace": cluster.uuid,
            "vip_port_ip": self.apiserver_address,
            "vip_external_ip": external_apiserver_address,
            "vip_port_id": port_info["port_id"],
            "service_ip_range": cluser_service_ip_range,
            "pod_ip_range": cluser_pod_ip_range,
            "ca_cert": ca_cert_encoded,
            "ca_key": ca_key_encoded,
            "subnet_id": cluster_template.fixed_subnet,
            "public_network_id": self.public_network_id,
            "cloud_provider_enabled": cloud_provider_enabled,
            "kube_version": cluster.labels.get("kube_tag", "v1.14.3"),
            "cloud_provider_tag": cluster.labels.get("cloud_provider_tag",
                                                     "v1.15.0")
        }

        # Keystone related info.
        osc = clients.OpenStackClients(context)
        params['trustee_user_id'] = cluster.trustee_user_id
        params['trustee_password'] = cluster.trustee_password
        if CONF.trust.cluster_user_trust:
            params['trust_id'] = cluster.trust_id
        else:
            params['trust_id'] = ""
        kwargs = {
            'service_type': 'identity',
            'interface': CONF.trust.trustee_keystone_interface,
            'version': 3
        }
        if CONF.trust.trustee_keystone_region_name:
            kwargs['region_name'] = CONF.trust.trustee_keystone_region_name
        params['auth_url'] = osc.url_for(**kwargs).rstrip('/')

        _apply_manifest = functools.partial(self._apply_manifest, params)

        LOG.info("Creating namespace for cluster %s", cluster.uuid)
        _apply_manifest('namespace.yaml.j2')

        # Create Secret for the new cluster CA and the kube services, the CA
        # could be referenced by various cluster components.
        LOG.info("Creating Secrets for cluster %s", cluster.uuid)
        _apply_manifest('secrets.yaml.j2')
        # TODO: Wait for all the certificates are ready

        # etcd Service and StatefulSet
        LOG.info("Creating etcd service for cluster %s", cluster.uuid)
        _apply_manifest('etcd.yaml.j2')

        # apiserver Service and Deployment
        LOG.info("Creating kube-apiserver for cluster %s", cluster.uuid)
        _apply_manifest('kube-apiserver.yaml.j2')

        # Deploy kube-controller-manager
        LOG.info("Creating kube-controller-manager for cluster %s",
                 cluster.uuid)
        _apply_manifest('kube-controllermgr.yaml.j2')

        # Deploy kube-scheduler
        LOG.info("Creating kube-scheduler for cluster %s", cluster.uuid)
        _apply_manifest('kube-scheduler.yaml.j2')

        kubeconfig_path = self._get_kubeconfig(
            context, cluster,
            ca_cert_encoded=ca_cert_encoded
        )
        LOG.info(
            "Kubeconfig created for cluster %s, path: %s",
            cluster.uuid, kubeconfig_path
        )

        cluster_kubectl = kubectl.KubeCtl(
            bin="/usr/bin/kubectl",
            global_flags="--kubeconfig %s" % kubeconfig_path
        )

        LOG.info(
            "Waiting for all the components up and running for "
            "cluster %s", cluster.uuid
        )
        self._wait_for_apiserver(cluster.uuid, cluster_kubectl)

        if cloud_provider_enabled:
            # Deploy openstack-cloud-controller-manager
            LOG.info("Creating openstack-cloud-controller-manager for "
                     "cluster %s", cluster.uuid)
            # Create RBAC for openstack-cloud-controller-manager in the
            # cluster.
            _apply_manifest(
                "openstack-cloud-controller-manager-in-cluster.yaml.j2",
                cluster_kubectl
            )
            _apply_manifest('openstack-cloud-controller-manager.yaml.j2')

        # Create bootstrap token and the bootstrap RBAC in the new cluster
        LOG.info(
            "Creating bootstrap token and RBAC in the cluster %s",
            cluster.uuid
        )
        expiration = timeutils.utcnow() + datetime.timedelta(days=1)
        # For bootstrap token, refer to
        # https://kubernetes.io/docs/reference/access-authn-authz/bootstrap-tokens/
        token_id = self._generate_random_string(6)
        token_secret = self._generate_random_string(16)
        bootstrap_params = {
            "token_id": token_id,
            "token_secret": token_secret,
            "expiration": expiration.strftime('%Y-%m-%dT%H:%M:%SZ'),
        }
        bootstrap_template = self.jinja_env.get_template('bootstrap.yaml.j2')
        bootstrap_body = bootstrap_template.render(bootstrap_params)
        cluster_kubectl.apply(definition=bootstrap_body)

        self.bootstrap_token = "%s.%s" % (token_id, token_secret)

        # Grant privilege to 'kubernetes' user so that apiserver can access
        # to kubelet for operations like logs, exec, etc.
        # The user name here must be the same with apiserver CN in
        # secrets.yaml.j2
        cluster_kubectl.execute(
            "create clusterrolebinding kube-apiserver --clusterrole "
            "cluster-admin --user kubernetes"
        )

        # Starts to create VMs and bootstrap kubelet
        LOG.info("Creating worker nodes for cluster %s", cluster.uuid)
        super(Driver, self).create_cluster(
            context, cluster, cluster_create_timeout
        )

    def pre_delete_cluster(self, context, cluster):
        """Delete cloud resources before deleting the cluster."""
        if keystone.is_octavia_enabled():
            LOG.info("Starting to delete Service loadbalancers for cluster %s",
                     cluster.uuid)
            octavia.delete_loadbalancers(context, cluster)

    def delete_cluster(self, context, cluster):
        LOG.info("Starting to delete cluster %s", cluster.uuid)

        self.pre_delete_cluster(context, cluster)

        c_template = conductor_utils.retrieve_cluster_template(
            context,
            cluster
        )

        # NOTE: The fake fields are only for the yaml file integrity and do not
        # affect the deletion
        params = {
            "namespace": cluster.uuid,
            "cloud_provider_tag": "fake",
            "kube_version": "fake",
        }
        _delete_manifest = functools.partial(self._delete_manifest, params)

        LOG.info("Deleting components for cluster %s", cluster.uuid)
        for tmpl in [
            "openstack-cloud-controller-manager.yaml.j2",
            "kube-scheduler.yaml.j2", "kube-controllermgr.yaml.j2",
            "kube-apiserver.yaml.j2", "etcd.yaml.j2",
            "secrets.yaml.j2", "namespace.yaml.j2"
        ]:
            _delete_manifest(tmpl)

        # Delete floating ip if needed.
        if (self._master_lb_fip_enabled(cluster, c_template) and
                cluster.api_address):
            network_client = clients.OpenStackClients(context).neutron()
            ip = netutils.urlsplit(cluster.api_address).netloc.split(":")[0]
            fips = network_client.list_floatingips(floating_ip_address=ip)
            for fip in fips['floatingips']:
                LOG.info("Deleting floating ip %s for cluster %s",
                         fip["floating_ip_address"], cluster.uuid)
                network_client.delete_floatingip(fip['id'])

        # Delete VIP port
        LOG.info("Deleting ports for cluster %s", cluster.uuid)
        tag = {"magnum": cluster.uuid}
        tags = [jsonutils.dumps(tag)]
        neutron.delete_port_by_tags(context, tags)

        # Delete Heat stack.
        if cluster.stack_id:
            LOG.info("Deleting Heat stack %s for cluster %s",
                     cluster.stack_id, cluster.uuid)
            self._delete_stack(
                context, clients.OpenStackClients(context), cluster
            )

    def _install_addons(self, cluster, cluster_kubectl, context):
        """Install add-on services.

        Including Calico, kube-proxy, CoreDNS
        """
        LOG.info("Starting to install add-ons for cluster %s", cluster.uuid)

        # Add initializing tag for the new cluster.
        tag_template = self.jinja_env.get_template('addon_tag.yaml.j2')
        tag_body = tag_template.render(
            {'namespace': cluster.uuid, 'status': 'initializing'}
        )
        self.kubectl.apply(definition=tag_body)

        cluster_template = conductor_utils.retrieve_cluster_template(
            context, cluster
        )
        if cluster_template.network_driver == 'flannel':
            cluser_pod_ip_range = cluster.labels.get(
                'flannel_network_cidr', '10.100.0.0/16'
            )
        if cluster_template.network_driver == 'calico':
            cluser_pod_ip_range = cluster.labels.get(
                'calico_ipv4pool', '192.168.0.0/16'
            )

        cluser_service_ip_range = cluster.labels.get(
            'service_cluster_ip_range', '10.97.0.0/16'
        )
        service_ip_net = netaddr.IPNetwork(cluser_service_ip_range)
        cluster_dns_service_ip = service_ip_net[10]
        params = {
            'apiserver_address': cluster.master_addresses[0],
            'cluster_id': cluster.uuid,
            'pod_ip_range': cluser_pod_ip_range,
            'cluster_dns_service_ip': cluster_dns_service_ip,
            "kube_version": cluster.labels.get("kube_tag", "v1.14.3"),
        }

        LOG.info(
            'Installing calico, proxy, coredns for cluster %s',
            cluster.uuid
        )
        for t in ['calico_node_rbac.yaml.j2', 'calico.yaml.j2',
                  'kube-proxy.yaml.j2', 'coredns.yaml.j2']:
            template = self.jinja_env.get_template(t)
            body = template.render(params)
            cluster_kubectl.apply(definition=body)

        # Add initialized tag for the new cluster.
        tag_template = self.jinja_env.get_template('addon_tag.yaml.j2')
        tag_body = tag_template.render(
            {'namespace': cluster.uuid, 'status': 'initialized'}
        )
        self.kubectl.apply(definition=tag_body)

    def _workers_ready(self, cluster, cluster_kubectl):
        """Check if all the workers in the cluster are ready."""
        ready_workers = 0

        try:
            nodes = cluster_kubectl.get("nodes")
            if len(nodes) == 0:
                return False
        except Exception:
            return False

        for node in nodes:
            conditions = node['status']['conditions']
            for c in conditions:
                if (c.get('type') == 'Ready' and
                        c.get('status') == 'True'):
                    ready_workers += 1

        if ready_workers == cluster.node_count:
            return True

        return False

    def _sync_cluster_status(self, cluster, stack):
        cluster.status = stack.stack_status
        cluster.status_reason = stack.stack_status_reason
        cluster.save()

    def update_cluster_status(self, context, cluster):
        """Updates the cluster status.

        This method should be finished within the periodic interval(10s).

        :param context: Admin context.
        :param cluster: Cluster object.
        """
        if cluster.status == fields.ClusterStatus.CREATE_IN_PROGRESS:
            if cluster.stack_id is None:
                return

            stack_ctx = mag_ctx.make_cluster_context(cluster)
            os_clients = clients.OpenStackClients(stack_ctx)
            stack = os_clients.heat().stacks.get(
                cluster.stack_id,
                resolve_outputs=False
            )

            if stack.stack_status == fields.ClusterStatus.CREATE_COMPLETE:
                stack_ctx = mag_ctx.make_cluster_context(cluster)
                kubeconfig_path = self._get_kubeconfig(stack_ctx, cluster)
                cluster_kubectl = kubectl.KubeCtl(
                    bin="/usr/bin/kubectl",
                    global_flags="--kubeconfig %s" % kubeconfig_path
                )

                ns = self.kubectl.get("namespace %s" % cluster.uuid)
                labels = ns['metadata'].get('labels', {})

                if not labels.get('magnum.k8s.io/status'):
                    self._install_addons(cluster, cluster_kubectl, context)
                    return

                if self._workers_ready(cluster, cluster_kubectl):
                    LOG.info(
                        'Cluster %s is created successfully', cluster.uuid
                    )

                    # Update the worker addresses in the cluster from the Heat
                    # stack output.
                    stack = os_clients.heat().stacks.get(
                        cluster.stack_id,
                        resolve_outputs=True
                    )
                    template_def = self.get_template_definition()
                    c_template = conductor_utils.retrieve_cluster_template(
                        context,
                        cluster
                    )
                    template_def.update_outputs(stack, c_template, cluster)

                    cluster.status = fields.ClusterStatus.CREATE_COMPLETE
                    cluster.save()
            elif stack.stack_status in (
                fields.ClusterStatus.CREATE_FAILED,
                fields.ClusterStatus.DELETE_FAILED,
                fields.ClusterStatus.UPDATE_FAILED,
                fields.ClusterStatus.ROLLBACK_COMPLETE,
                fields.ClusterStatus.ROLLBACK_FAILED
            ):
                self._sync_cluster_status(cluster, stack)
                LOG.error('Failed to create cluster %s', cluster.uuid)

        elif cluster.status == fields.ClusterStatus.DELETE_IN_PROGRESS:
            # Check if the namespace is deleted.
            ns_template = self.jinja_env.get_template('namespace.yaml.j2')
            ns_body = ns_template.render({"namespace": cluster.uuid})
            namespaces = self.kubectl.get('namespace')
            names = [n['metadata']['name'] for n in namespaces]

            if cluster.uuid not in names:
                LOG.debug(
                    "Namespace has been deleted for cluster %s",
                    cluster.uuid
                )
                stack_ctx = mag_ctx.make_cluster_context(cluster)
                os_client = clients.OpenStackClients(stack_ctx)

                try:
                    trust_manager.delete_trustee_and_trust(
                        os_client,
                        context,
                        cluster
                    )
                    cert_manager.delete_certificates_from_cluster(
                        cluster,
                        context=context
                    )
                    cert_manager.delete_client_files(cluster, context=context)
                except exception.ClusterNotFound:
                    LOG.info(
                        'The cluster %s has been deleted by others.',
                        cluster.uuid
                    )

                LOG.info('Cluster %s has been deleted.', cluster.uuid)

                cluster.status = fields.ClusterStatus.DELETE_COMPLETE
                cluster.save()

    def upgrade_cluster(self, context, cluster, cluster_template,
                        max_batch_size, nodegroup, scale_manager=None,
                        rollback=False):
        raise NotImplementedError("Must implement 'upgrade_cluster'")
