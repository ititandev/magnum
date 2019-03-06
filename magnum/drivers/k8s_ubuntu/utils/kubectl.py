import json
import subprocess

from oslo_log import log as logging

from magnum.common import exception

LOG = logging.getLogger(__name__)


class KubeCtl(object):
    def __init__(self, bin='kubectl', global_flags=''):
        super(KubeCtl, self).__init__()
        self.kubectl = '{} {}'.format(bin, global_flags)

    def execute(self, command, definition=None, namespace=None,
                print_error=True):
        if definition:
            cmd = "cat <<'EOF' | {} {} -f -\n{}\nEOF".format(
                self.kubectl, command, definition
            )
        else:
            if namespace:
                cmd = "{} -n {} {}".format(self.kubectl, namespace, command)
            else:
                cmd = "{} {}".format(self.kubectl, command)

        try:
            return subprocess.check_output(cmd, shell=True)
        except subprocess.CalledProcessError:
            if print_error:
                if "delete" in command:
                    LOG.warning("K8s: Delete failed.")
                else:
                    exc_msg = "Failed to execute kubectl command, cmd=%s" % cmd
                    LOG.error(exc_msg)
                    raise exception.MagnumException(message=exc_msg)

    def apply(self, *args, **kwargs):
        return self.execute('apply', *args, **kwargs)

    def delete(self, *args, **kwargs):
        return self.execute('delete', *args, **kwargs)

    def get(self, resource, namespace=None, **kwargs):
        result = self.execute(
            'get %s -o json' % resource, namespace=namespace, **kwargs
        ).decode()

        ret = json.loads(result)
        if 'items' in ret:
            return ret['items']

        return ret

    def describe(self, *args, **kwargs):
        return self.execute('describe', *args, **kwargs)

    def batch_delete(self, resource_mapping=[]):
        """Deletes Kubernetes resources.

        Example for the resource_mapping param:
        [{"service": ["srv1", "srv2"]}, {"deployment": ["deploy1"]}]

        Be careful to the deletion order.
        """
        for res in resource_mapping:
            for res_type, items in res.items():
                resources = " ".join(items)
                self.execute("delete %s %s" % (res_type, resources))
