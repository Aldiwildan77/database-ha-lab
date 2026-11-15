// Creates the benchmark schema and loads a deterministic dataset (same rows + seed => same data).
import java.sql.*

def LabDb = new GroovyClassLoader(this.class.classLoader)
    .parseClass(new File(props.getProperty('lab_script_dir', 'jmeter/scripts'), 'LabDb.groovy'))
int rows = Integer.parseInt(props.getProperty('rows', '100000'))
long seed = Long.parseLong(props.getProperty('seed', '42'))
String db = props.getProperty('database', 'halab')

Properties p = new Properties(); p.putAll(props); p.setProperty('database', '')
Map c = LabDb.connect(p, true)
Connection conn = c.conn
conn.setAutoCommit(true)
Statement st = conn.createStatement()
st.execute("CREATE DATABASE IF NOT EXISTS `${db}`")
st.execute("USE `${db}`")
st.execute('''CREATE TABLE IF NOT EXISTS accounts (
  id BIGINT PRIMARY KEY, balance BIGINT NOT NULL, payload VARCHAR(255) NOT NULL,
  updated_at DATETIME(6) NOT NULL) ENGINE=InnoDB''')
st.execute('''CREATE TABLE IF NOT EXISTS ha_ledger (
  txn_id VARCHAR(96) PRIMARY KEY, run_id VARCHAR(48) NOT NULL, thread VARCHAR(48) NOT NULL,
  seq BIGINT NOT NULL, client_epoch_ms BIGINT NOT NULL, served_by VARCHAR(255) NOT NULL,
  server_ts DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6), payload VARCHAR(255) NOT NULL,
  KEY run_idx (run_id)) ENGINE=InnoDB''')
st.execute('''CREATE TABLE IF NOT EXISTS dataset_info (
  id TINYINT PRIMARY KEY, row_count BIGINT NOT NULL, seed BIGINT NOT NULL, loaded_at DATETIME(6) NOT NULL) ENGINE=InnoDB''')

ResultSet rs = st.executeQuery('SELECT row_count, seed FROM dataset_info WHERE id = 1')
boolean current = rs.next() && rs.getLong(1) == rows && rs.getLong(2) == seed
rs.close()
if (!current) {
    log.info("prepare: loading ${rows} rows with seed ${seed}")
    st.execute('DELETE FROM accounts')
    conn.setAutoCommit(false)
    PreparedStatement ps = conn.prepareStatement('INSERT INTO accounts (id, balance, payload, updated_at) VALUES (?,?,?,?)')
    Timestamp epoch = Timestamp.valueOf('2026-01-01 00:00:00')
    for (long id = 1; id <= rows; id++) {
        long h = (id * 6364136223846793005L + seed) ^ (seed << 13)
        ps.setLong(1, id)
        ps.setLong(2, Math.floorMod(h, 1_000_000L))
        ps.setString(3, (Long.toHexString(h) * 8).take(128))
        ps.setTimestamp(4, epoch)
        ps.addBatch()
        if (id % 1000 == 0 || id == rows) { ps.executeBatch(); conn.commit() }
    }
    ps.close()
    conn.setAutoCommit(true)
    st.execute("REPLACE INTO dataset_info VALUES (1, ${rows}, ${seed}, NOW(6))")
} else {
    log.info("prepare: dataset already loaded (${rows} rows, seed ${seed})")
}
st.close(); conn.close()
SampleResult.setSuccessful(true)
SampleResult.setResponseMessage(current ? 'dataset present' : "loaded ${rows} rows")
