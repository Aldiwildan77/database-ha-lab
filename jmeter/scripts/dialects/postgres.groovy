// PostgreSQL dialect. Errors are classified by SQLSTATE, not by vendor code.
class LabDialect {
    String url(String hostPort, String db, Properties p) {
        int connectS = Math.max(1, (int) (Long.parseLong(p.getProperty('connect_timeout_ms', '3000')) / 1000))
        int socketS = Math.max(1, (int) (Long.parseLong(p.getProperty('socket_timeout_ms', '15000')) / 1000))
        String ssl = p.getProperty('ssl_mode', 'PREFERRED') == 'REQUIRED' ? 'require' : 'prefer'
        return "jdbc:postgresql://${hostPort}/${db}?sslmode=${ssl}&connectTimeout=${connectS}&socketTimeout=${socketS}" +
               "&tcpKeepAlive=true&reWriteBatchedInserts=true"
    }

    String adminDatabase() { return 'postgres' }

    // halab.node (and cluster_name outside Patroni) is set to the node's address by the postgres role, so served_by matches the inventory.
    String roQuery() { return "SELECT CASE WHEN pg_is_in_recovery() OR current_setting('default_transaction_read_only') = 'on' THEN 1 ELSE 0 END, COALESCE(current_setting('halab.node', true), current_setting('cluster_name'))" }

    String now() { return 'clock_timestamp()' }

    void createDatabase(java.sql.Connection conn, String db) {
        boolean exists = conn.prepareStatement('SELECT 1 FROM pg_database WHERE datname = ?').withCloseable { ps ->
            ps.setString(1, db); ps.executeQuery().withCloseable { it.next() }
        }
        if (!exists) conn.createStatement().withCloseable { it.execute("CREATE DATABASE \"${db}\"") }
    }

    List<String> schema() {
        return ['''CREATE TABLE IF NOT EXISTS accounts (
  id BIGINT PRIMARY KEY, balance BIGINT NOT NULL, payload VARCHAR(255) NOT NULL,
  updated_at TIMESTAMP(6) NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS ha_ledger (
  txn_id VARCHAR(96) PRIMARY KEY, run_id VARCHAR(48) NOT NULL, thread VARCHAR(48) NOT NULL,
  seq BIGINT NOT NULL, client_epoch_ms BIGINT NOT NULL, served_by VARCHAR(255) NOT NULL,
  server_ts TIMESTAMP(6) NOT NULL DEFAULT clock_timestamp(), payload VARCHAR(255) NOT NULL)''',
                'CREATE INDEX IF NOT EXISTS run_idx ON ha_ledger (run_id)',
                '''CREATE TABLE IF NOT EXISTS dataset_info (
  id SMALLINT PRIMARY KEY, row_count BIGINT NOT NULL, seed BIGINT NOT NULL, loaded_at TIMESTAMP(6) NOT NULL)''']
    }

    String upsertDatasetInfo(long rows, long seed) {
        return "INSERT INTO dataset_info VALUES (1, ${rows}, ${seed}, clock_timestamp()) " +
               'ON CONFLICT (id) DO UPDATE SET row_count = EXCLUDED.row_count, seed = EXCLUDED.seed, loaded_at = EXCLUDED.loaded_at'
    }

    String classifySql(int code, String state) {
        switch (state) {
            case '25006': return 'read_only'
            case '40P01': return 'deadlock'
            case '55P03': return 'lock_timeout'
            case '23505': return 'duplicate'
            case '40001': return 'conflict'
            case '57P03': return 'not_ready'
            case '57P01':
            case '57P02': return 'connection'
        }
        return null
    }
}
