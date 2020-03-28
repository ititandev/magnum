#!/usr/bin/env bash

set -e

mkdir -p /var/lib/kubelet /var/lib/kubelet/pki/
rm -rf /etc/systemd/system/kubelet.service.d
BOOTSTRAP_CONFIG=/var/lib/kubelet/bootstrap-kubeconfig
CA_FILE=/var/lib/kubelet/pki/ca.crt
CAKEY_FILE=/var/lib/kubelet/pki/ca.key
KUBELET_CONFIG=/var/lib/kubelet/config.yaml
DOCKER_VERSION=17.03.2~ce-0~ubuntu-xenial
CLOUD_ENABLED=${CLOUD_PROVIDER_ENABLED}

echo "Installing docker..."
apt-get update -qq
apt-get install -y -qq apt-transport-https prips ca-certificates
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo apt-key add -
add-apt-repository "deb [arch=amd64] https://download.docker.com/linux/ubuntu xenial stable"
apt-get update -qq
apt-get install -y -qq docker-ce=${DOCKER_VERSION}
echo "Finished to install docker"

echo "Installing kubelet..."
curl -sSL https://packages.cloud.google.com/apt/doc/apt-key.gpg | apt-key add -
add-apt-repository "deb http://apt.kubernetes.io/ kubernetes-xenial main"
apt-get update -qq
apt install -y kubernetes-cni=0.6.0-00 kubelet=${KUBE_VERSION}-00 kubectl=${KUBE_VERSION}-00
echo "Finished to install kubelet"

# Create CA files
echo "Writing CA files"
cat <<EOF > ${CA_FILE}
${CA_CONTENT}
EOF
cat <<EOF > ${CAKEY_FILE}
${CAKEY_CONTENT}
EOF

echo "Downloading cfssl tooling"
for bin in "cfssl" "cfssljson" "cfssl-certinfo"; do
  curl -sLO# https://pkg.cfssl.org/R1.2/${bin}_linux-amd64
  chmod +x ${bin}_linux-amd64
  mv ${bin}_linux-amd64 /usr/local/bin/${bin}
done

cat <<EOF > /var/lib/kubelet/pki/ca-config.json
{
  "signing": {
    "default": {
      "expiry": "8760h"
    },
    "profiles": {
      "kubernetes": {
        "usages": ["signing", "key encipherment", "server auth", "client auth"],
        "expiry": "43800h"
      }
    }
  }
}
EOF

hostname=$(hostname)
ip=$(ip route get 8.8.8.8 | head -1 | awk '{print $7}')
cat <<EOF > /var/lib/kubelet/pki/kubelet-config.json
{
  "CN": "system:node:${hostname}",
  "key": {
    "algo": "rsa",
    "size": 2048
  },
  "names": [
    {
      "C": "NZ",
      "L": "Wellington",
      "O": "system:nodes",
      "OU": "Kubelet server",
      "ST": "Newlands"
    }
  ]
}
EOF

pushd /var/lib/kubelet/pki
cfssl gencert \
  -ca=${CA_FILE} \
  -ca-key=${CAKEY_FILE} \
  -config=/var/lib/kubelet/pki/ca-config.json \
  -hostname=${hostname},${ip} \
  -profile=kubernetes \
  /var/lib/kubelet/pki/kubelet-config.json | cfssljson -bare kubelet
popd

# Construct the kubelet bootstrap config file
echo "Preparing the kubelet bootstrap config file"
kubectl config set-cluster ${CLUSTER_ID} --server https://${KUBE_APISERVER}:6443 --certificate-authority=${CA_FILE} --embed-certs=true --kubeconfig=${BOOTSTRAP_CONFIG}
kubectl config set-credentials ${WORKER_NAME} --token=${BOOTSTRAP_TOKEN} --kubeconfig=${BOOTSTRAP_CONFIG}
kubectl config set-context default --cluster ${CLUSTER_ID} --user ${WORKER_NAME} --kubeconfig=${BOOTSTRAP_CONFIG}
kubectl config use-context default --kubeconfig=${BOOTSTRAP_CONFIG}

cat <<EOF > ${KUBELET_CONFIG}
kind: KubeletConfiguration
apiVersion: kubelet.config.k8s.io/v1beta1
authentication:
  anonymous:
    enabled: false
  webhook:
    enabled: true
  x509:
    clientCAFile: "${CA_FILE}"
authorization:
  mode: Webhook
clusterDomain: "cluster.local"
clusterDNS:
  - "${DNS_SERVICE_IP}"
rotateCertificates: true
runtimeRequestTimeout: "15m"
serverTLSBootstrap: true
tlsCertFile: "/var/lib/kubelet/pki/kubelet.pem"
tlsPrivateKeyFile: "/var/lib/kubelet/pki/kubelet-key.pem"
EOF

KUBELET_ARGS="--bootstrap-kubeconfig=${BOOTSTRAP_CONFIG} --kubeconfig=/var/lib/kubelet/kubeconfig --config=${KUBELET_CONFIG} --network-plugin=cni --register-node=true --v=2"

if [[ "${CLOUD_ENABLED^^}" = "TRUE" ]]; then
  cat <<EOF > /etc/kubernetes/cloud-config
[Global]
auth-url=${AUTH_URL}
user-id=${TRUSTEE_USER_ID}
password=${TRUSTEE_PASSWORD}
trust-id=${TRUST_ID}
[BlockStorage]
bs-version=v2
EOF
  KUBELET_ARGS="${KUBELET_ARGS} --cloud-provider=external"
fi

cat <<EOF > /etc/systemd/system/kubelet.service
[Unit]
Description=Kubernetes Kubelet
Documentation=https://github.com/kubernetes/kubernetes
After=docker.service
Requires=docker.service

[Service]
ExecStart=/usr/bin/kubelet ${KUBELET_ARGS}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "Restarting kubelet..."
systemctl daemon-reload; systemctl restart kubelet

${WC_NOTIFY} --data-binary '{"status": "SUCCESS"}'
echo "Signal sent."
