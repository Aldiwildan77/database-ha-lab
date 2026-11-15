#!/usr/bin/env bash
# Installs Apache JMeter and MySQL Connector/J into local/tools (checksums verified).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS="${ROOT}/local/tools"
JMETER_VERSION=5.6.3
JMETER_SHA512=5978a1a35edb5a7d428e270564ff49d2b1b257a65e17a759d259a9283fc17093e522fe46f474a043864aea6910683486340706d745fcdf3db1505fd71e689083
CONNECTOR_VERSION=26.10.0

command -v java >/dev/null || { echo "Java 8+ is required (java not found)"; exit 1; }
mkdir -p "$TOOLS"
cd "$TOOLS"

if [[ ! -x "apache-jmeter-${JMETER_VERSION}/bin/jmeter" ]]; then
  curl -fsSLO "https://archive.apache.org/dist/jmeter/binaries/apache-jmeter-${JMETER_VERSION}.tgz"
  echo "${JMETER_SHA512}  apache-jmeter-${JMETER_VERSION}.tgz" | shasum -a 512 -c -
  tar xzf "apache-jmeter-${JMETER_VERSION}.tgz"
  rm "apache-jmeter-${JMETER_VERSION}.tgz"
fi

jar="apache-jmeter-${JMETER_VERSION}/lib/mysql-connector-j-${CONNECTOR_VERSION}.jar"
if [[ ! -f "$jar" ]]; then
  base="https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/${CONNECTOR_VERSION}/mysql-connector-j-${CONNECTOR_VERSION}.jar"
  curl -fsSL -o "$jar" "$base"
  echo "$(curl -fsSL "${base}.sha1")  ${jar}" | shasum -a 1 -c -
fi
ln -sfn "apache-jmeter-${JMETER_VERSION}" jmeter
echo "JMeter ready: ${TOOLS}/jmeter/bin/jmeter"
