// Creates the benchmark schema and loads a deterministic dataset (same rows + seed => same data).
import java.sql.*

def LabDb = new GroovyClassLoader(this.class.classLoader)
    .parseClass(new File(props.getProperty('lab_script_dir', 'jmeter/scripts'), 'LabDb.groovy'))
int rows = Integer.parseInt(props.getProperty('rows', '100000'))
long seed = Long.parseLong(props.getProperty('seed', '42'))
String db = props.getProperty('database', 'halab')

def D = LabDb.dialect(props)
Properties p = new Properties(); p.putAll(props); p.setProperty('database', D.adminDatabase())
Map admin = LabDb.connect(p, true)
admin.conn.setAutoCommit(true)
D.createDatabase(admin.conn, db)
admin.conn.close()
Map c = LabDb.connect(props, true)
Connection conn = c.conn
conn.setAutoCommit(true)
Statement st = conn.createStatement()
D.schema().each { st.execute(it) }

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
    st.execute(D.upsertDatasetInfo(rows, seed))
} else {
    log.info("prepare: dataset already loaded (${rows} rows, seed ${seed})")
}
st.close(); conn.close()
SampleResult.setSuccessful(true)
SampleResult.setResponseMessage(current ? 'dataset present' : "loaded ${rows} rows")
