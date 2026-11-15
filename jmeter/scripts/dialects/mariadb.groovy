// MariaDB dialect. UNTESTED: the mariadb engine is planned (see ansible/engines/mariadb/engine.yml).
class LabDialect {
    String url(String hostPort, String db, Properties p) {
        String connectMs = p.getProperty('connect_timeout_ms', '3000')
        String socketMs = p.getProperty('socket_timeout_ms', '15000')
        String ssl = p.getProperty('ssl_mode', 'PREFERRED') == 'REQUIRED' ? 'trust' : 'disable'
        return "jdbc:mariadb://${hostPort}/${db}?sslMode=${ssl}&connectTimeout=${connectMs}&socketTimeout=${socketMs}" +
               "&tcpKeepAlive=true&useServerPrepStmts=false"
    }

    String adminDatabase() { return '' }

    String roQuery() { return 'SELECT @@global.read_only, COALESCE(NULLIF(@@report_host, \'\'), @@hostname)' }

    String now() { return 'NOW(6)' }

    void createDatabase(java.sql.Connection conn, String db) {
        conn.createStatement().withCloseable { it.execute("CREATE DATABASE IF NOT EXISTS `${db}`") }
    }

    List<String> schema() {
        return ['''CREATE TABLE IF NOT EXISTS accounts (
  id BIGINT PRIMARY KEY, balance BIGINT NOT NULL, payload VARCHAR(255) NOT NULL,
  updated_at DATETIME(6) NOT NULL) ENGINE=InnoDB''',
                '''CREATE TABLE IF NOT EXISTS ha_ledger (
  txn_id VARCHAR(96) PRIMARY KEY, run_id VARCHAR(48) NOT NULL, thread VARCHAR(48) NOT NULL,
  seq BIGINT NOT NULL, client_epoch_ms BIGINT NOT NULL, served_by VARCHAR(255) NOT NULL,
  server_ts DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), payload VARCHAR(255) NOT NULL,
  KEY run_idx (run_id)) ENGINE=InnoDB''',
                '''CREATE TABLE IF NOT EXISTS dataset_info (
  id TINYINT PRIMARY KEY, row_count BIGINT NOT NULL, seed BIGINT NOT NULL, loaded_at DATETIME(6) NOT NULL) ENGINE=InnoDB''']
    }

    String upsertDatasetInfo(long rows, long seed) {
        return "REPLACE INTO dataset_info VALUES (1, ${rows}, ${seed}, NOW(6))"
    }

    String classifySql(int code, String state) {
        if (code == 1290 || code == 1836) return 'read_only'
        if (code == 1213) return 'deadlock'
        if (code == 1205) return 'lock_timeout'
        if (code == 1062) return 'duplicate'
        if (code == 1047) return 'not_ready'
        return null
    }
}
