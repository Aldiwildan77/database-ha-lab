// One benchmark transaction per sample. Timings use System.nanoTime() (monotonic).
// Parameters: <default read ratio>. JMeter properties override it (read_ratio).
import java.sql.*
import java.util.concurrent.ThreadLocalRandom

def LabDb = vars.getObject('LabDb')
if (LabDb == null) {
    LabDb = new GroovyClassLoader(this.class.classLoader)
        .parseClass(new File(props.getProperty('lab_script_dir', 'jmeter/scripts'), 'LabDb.groovy'))
    vars.putObject('LabDb', LabDb)
}

def D = LabDb.dialect(props)
long t0 = System.nanoTime()
double readRatio = Double.parseDouble(props.getProperty('read_ratio', args ? args[0] : '0.5'))
int rows = Integer.parseInt(props.getProperty('rows', '100000'))
int readsPerTxn = Integer.parseInt(props.getProperty('reads_per_txn', '5'))
String runId = props.getProperty('run_id', 'adhoc')
String workload = props.getProperty('workload', 'oltp')   // oltp | orders
int items = Integer.parseInt(props.getProperty('items', '10000'))
ThreadLocalRandom rnd = ThreadLocalRandom.current()
String group = ctx.getThreadGroup().getName()
String op = group == 'report' ? 'report' : (rnd.nextDouble() < readRatio ? 'read' : 'write')
String thread = group + '-' + ctx.getThreadNum()
long seq = (vars.getObject('seq') ?: 0L) + 1
vars.putObject('seq', seq)
String txnId = op == 'write' ? "${runId}-${thread}-${seq}" : ''

long connectNs = 0, execNs = 0, commitNs = 0, rollbackNs = 0
String errClass = '', msg = 'OK'
Map c = vars.getObject('db')
boolean commitSent = false
try {
    if (c == null || c.conn.isClosed()) {
        long c0 = System.nanoTime()
        try {
            c = LabDb.connect(props, true)
        } finally {
            connectNs = System.nanoTime() - c0
        }
        vars.putObject('db', c)
    }
    Connection conn = c.conn
    long e0 = System.nanoTime()
    if (op == 'report') {
        // Reporting query next to OLTP: aggregate over a 10% id range (report_threads > 0).
        long span = Math.max(1L, (long) (rows / 10))
        long lo = rnd.nextLong(1, Math.max(2L, rows - span + 1))
        PreparedStatement ps = conn.prepareStatement('SELECT COUNT(*), SUM(balance), AVG(balance) FROM accounts WHERE id BETWEEN ? AND ?')
        ps.setLong(1, lo); ps.setLong(2, lo + span)
        ResultSet rs = ps.executeQuery(); while (rs.next()) {}; rs.close(); ps.close()
    } else if (workload == 'orders' && op == 'read') {
        PreparedStatement ps = conn.prepareStatement(
            'SELECT o.txn_id, o.total, SUM(l.qty) FROM orders o JOIN order_lines l ON l.txn_id = o.txn_id ' +
            'WHERE o.customer_id = ? GROUP BY o.txn_id, o.total, o.created_ms ORDER BY o.created_ms DESC LIMIT 10')
        ps.setLong(1, rnd.nextLong(1, rows + 1))
        ResultSet rs = ps.executeQuery(); while (rs.next()) {}; rs.close(); ps.close()
        PreparedStatement sp = conn.prepareStatement('SELECT qty FROM stock WHERE item_id = ?')
        for (int i = 0; i < 3; i++) {
            sp.setLong(1, rnd.nextLong(1, items + 1)); ResultSet r2 = sp.executeQuery(); while (r2.next()) {}; r2.close()
        }
        sp.close()
    } else if (workload == 'orders') {
        writeLedger(conn, txnId, runId, thread, seq, c.servedBy)
        long customer = rnd.nextLong(1, rows + 1)
        List<Long> picked = (1..3).collect { rnd.nextLong(1, items + 1) }.unique().sort()   // fixed lock order
        long total = 0
        PreparedStatement ln = conn.prepareStatement('INSERT INTO order_lines (txn_id, line_no, item_id, qty) VALUES (?,?,?,?)')
        PreparedStatement sk = conn.prepareStatement('UPDATE stock SET qty = qty - ? WHERE item_id = ?')
        picked.eachWithIndex { Long item, int i ->
            int qty = rnd.nextInt(1, 6); total += qty * 100
            ln.setString(1, txnId); ln.setInt(2, i + 1); ln.setLong(3, item); ln.setInt(4, qty); ln.executeUpdate()
            sk.setInt(1, qty); sk.setLong(2, item); sk.executeUpdate()
        }
        ln.close(); sk.close()
        PreparedStatement od = conn.prepareStatement('INSERT INTO orders (txn_id, customer_id, total, created_ms) VALUES (?,?,?,?)')
        od.setString(1, txnId); od.setLong(2, customer); od.setLong(3, total); od.setLong(4, System.currentTimeMillis())
        od.executeUpdate(); od.close()
        PreparedStatement up = conn.prepareStatement(
            "UPDATE accounts SET balance = balance + ?, updated_at = ${D.now()} WHERE id = ?".toString())
        up.setLong(1, total); up.setLong(2, customer); up.executeUpdate(); up.close()
    } else if (op == 'read') {
        PreparedStatement ps = conn.prepareStatement('SELECT id, balance, payload FROM accounts WHERE id = ?')
        for (int i = 0; i < readsPerTxn; i++) {
            ps.setLong(1, rnd.nextLong(1, rows + 1))
            ResultSet rs = ps.executeQuery(); while (rs.next()) {}; rs.close()
        }
        ps.close()
    } else {
        writeLedger(conn, txnId, runId, thread, seq, c.servedBy)
        PreparedStatement up = conn.prepareStatement(
            "UPDATE accounts SET balance = balance + 1, updated_at = ${D.now()} WHERE id = ?".toString())
        up.setLong(1, rnd.nextLong(1, rows + 1)); up.executeUpdate(); up.close()
    }
    execNs = System.nanoTime() - e0
    long k0 = System.nanoTime()
    commitSent = true
    conn.commit()
    commitNs = System.nanoTime() - k0
    SampleResult.setSuccessful(true)
} catch (Throwable t) {
    errClass = LabDb.classify(t, D)
    // Connection lost during COMMIT: the server may or may not have committed.
    if (commitSent && errClass in ['connection', 'timeout']) errClass = 'commit_unknown'
    msg = (t.getMessage() ?: t.class.simpleName).replaceAll('\\s+', ' ').take(200)
    if (c?.conn != null) {
        long r0 = System.nanoTime()
        try { if (!c.conn.isClosed()) c.conn.rollback() } catch (ignored) {}
        rollbackNs = System.nanoTime() - r0
        if (LabDb.dropsConnection(errClass)) {
            try { c.conn.close() } catch (ignored) {}
            vars.putObject('db', null)
        }
    }
    SampleResult.setSuccessful(false)
}
long txnNs = System.nanoTime() - t0

// Stale-read check (stale_check=1): how long until this committed write is visible on a replica.
// Runs after the timing above, so it lowers throughput but not the measured latency.
String visibleMs = '', staleNode = ''
if (props.getProperty('stale_check', '0') == '1' && op == 'write' && !errClass) {
    Map r = vars.getObject('replica')
    try {
        if (r == null || r.conn.isClosed()) { r = LabDb.connectReplica(props); vars.putObject('replica', r) }
        if (r != null) {
            staleNode = r.servedBy
            long window = Long.parseLong(props.getProperty('stale_window_ms', '2000'))
            long s0 = System.nanoTime()
            PreparedStatement q = r.conn.prepareStatement('SELECT 1 FROM ha_ledger WHERE txn_id = ?')
            q.setString(1, txnId)
            while (true) {
                ResultSet rs = q.executeQuery(); boolean seen = rs.next(); rs.close()
                long el = (long) ((System.nanoTime() - s0) / 1_000_000)
                if (seen) { visibleMs = el as String; break }
                if (el > window) { visibleMs = '-1'; break }
                Thread.sleep(5)
            }
            q.close()
        }
    } catch (Throwable ignored) {
        try { r?.conn?.close() } catch (ignored2) {}
        vars.putObject('replica', null)
    }
}
vars.put('visible_ms', visibleMs)
vars.put('stale_node', staleNode)

SampleResult.setSampleLabel(op)
SampleResult.setResponseCode(errClass ?: '200')
SampleResult.setResponseMessage(msg)
SampleResult.setConnectTime((long) (connectNs / 1_000_000))
vars.put('txn_id', txnId)
vars.put('op', op)
vars.put('connect_ns', connectNs as String)
vars.put('exec_ns', execNs as String)
vars.put('commit_ns', commitNs as String)
vars.put('rollback_ns', rollbackNs as String)
vars.put('txn_ns', txnNs as String)
vars.put('served_by', c?.servedBy ?: '')
vars.put('err_class', errClass)
vars.put('endpoint', c?.endpoint ?: '')

long thinkMs = Long.parseLong(props.getProperty(op == 'report' ? 'report_think_ms' : 'think_ms', op == 'report' ? '1000' : '0'))
if (thinkMs > 0) Thread.sleep(thinkMs)
// Back off while the endpoint is unavailable so an outage does not flood the JTL.
if (errClass && vars.getObject('db') == null) Thread.sleep(Long.parseLong(props.getProperty('reconnect_backoff_ms', '100')))

void writeLedger(Connection conn, String txnId, String runId, String thread, long seq, String servedBy) {
    PreparedStatement ins = conn.prepareStatement(
        'INSERT INTO ha_ledger (txn_id, run_id, thread, seq, client_epoch_ms, served_by, payload) VALUES (?,?,?,?,?,?,?)')
    ins.setString(1, txnId); ins.setString(2, runId); ins.setString(3, thread); ins.setLong(4, seq)
    ins.setLong(5, System.currentTimeMillis()); ins.setString(6, servedBy); ins.setString(7, 'w' * 64)
    ins.executeUpdate(); ins.close()
}
