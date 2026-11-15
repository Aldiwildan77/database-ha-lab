// MySQL dialect: every engine file defines class LabDialect with the same methods (see LabDb.groovy).
class LabDialect {
    String url(String hostPort, String db, Properties p) {
        String connectMs = p.getProperty('connect_timeout_ms', '3000')
        String socketMs = p.getProperty('socket_timeout_ms', '15000')
        String sslMode = p.getProperty('ssl_mode', 'PREFERRED')
        return "jdbc:mysql://${hostPort}/${db}?sslMode=${sslMode}&allowPublicKeyRetrieval=true" +
               "&connectTimeout=${connectMs}&socketTimeout=${socketMs}&tcpKeepAlive=true" +
               "&rewriteBatchedStatements=true&createDatabaseIfNotExist=false&useServerPrepStmts=false"
    }

    // Database to connect to before the benchmark database exists.
    String adminDatabase() { return '' }

    // Returns [read_only (0/1), served_by] for the connected node.
    String roQuery() { return 'SELECT @@global.read_only OR @@global.super_read_only, COALESCE(NULLIF(@@report_host, \'\'), @@hostname)' }

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

    // Error class for a SQLException, or null to fall through to the generic rules.
    String classifySql(int code, String state) {
        if (code == 1290 || code == 1836) return 'read_only'
        if (code == 1213) return 'deadlock'
        if (code == 1205) return 'lock_timeout'
        if (code == 1062) return 'duplicate'
        if (code == 3100 || code == 3101) return 'conflict'
        if (code == 1047 || code == 3796) return 'not_ready'
        return null
    }
}
