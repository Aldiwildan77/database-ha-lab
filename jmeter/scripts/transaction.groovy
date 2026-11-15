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

long t0 = System.nanoTime()
double readRatio = Double.parseDouble(props.getProperty('read_ratio', args ? args[0] : '0.5'))
int rows = Integer.parseInt(props.getProperty('rows', '100000'))
int readsPerTxn = Integer.parseInt(props.getProperty('reads_per_txn', '5'))
String runId = props.getProperty('run_id', 'adhoc')
ThreadLocalRandom rnd = ThreadLocalRandom.current()
String op = rnd.nextDouble() < readRatio ? 'read' : 'write'
String thread = ctx.getThreadGroup().getName() + '-' + ctx.getThreadNum()
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
    if (op == 'read') {
        PreparedStatement ps = conn.prepareStatement('SELECT id, balance, payload FROM accounts WHERE id = ?')
        for (int i = 0; i < readsPerTxn; i++) {
            ps.setLong(1, rnd.nextLong(1, rows + 1))
            ResultSet rs = ps.executeQuery(); while (rs.next()) {}; rs.close()
        }
        ps.close()
    } else {
        PreparedStatement ins = conn.prepareStatement(
            'INSERT INTO ha_ledger (txn_id, run_id, thread, seq, client_epoch_ms, served_by, payload) VALUES (?,?,?,?,?,?,?)')
        ins.setString(1, txnId); ins.setString(2, runId); ins.setString(3, thread); ins.setLong(4, seq)
        ins.setLong(5, System.currentTimeMillis()); ins.setString(6, c.servedBy); ins.setString(7, 'w' * 64)
        ins.executeUpdate(); ins.close()
        PreparedStatement up = conn.prepareStatement(
            'UPDATE accounts SET balance = balance + 1, updated_at = NOW(6) WHERE id = ?')
        up.setLong(1, rnd.nextLong(1, rows + 1)); up.executeUpdate(); up.close()
    }
    execNs = System.nanoTime() - e0
    long k0 = System.nanoTime()
    commitSent = true
    conn.commit()
    commitNs = System.nanoTime() - k0
    SampleResult.setSuccessful(true)
} catch (Throwable t) {
    errClass = LabDb.classify(t)
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

long thinkMs = Long.parseLong(props.getProperty('think_ms', '0'))
if (thinkMs > 0) Thread.sleep(thinkMs)
// Back off while the endpoint is unavailable so an outage does not flood the JTL.
if (errClass && vars.getObject('db') == null) Thread.sleep(Long.parseLong(props.getProperty('reconnect_backoff_ms', '100')))
