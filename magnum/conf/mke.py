# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from oslo_config import cfg

mke_group = cfg.OptGroup(name='mke', title='Options for the managed kubernetes engine (mke)')

mke_opts = [
    cfg.StrOpt('admin_kubeconfig',
               default='/etc/magnum/admin_kubeconfig',
               help='Kubeconfig file used to take with admin cluster.')
]


def register_opts(conf):
    conf.register_group(mke_group)
    conf.register_opts(mke_opts, group=mke_group)


def list_opts():
    return {
        mke_group: mke_opts
    }
