// Shared JDBC helpers, loaded by transaction.groovy and prepare.groovy via GroovyShell.
import java.sql.*

class LabDb {
    static Map dialects = [:]

    // Engine SQL differences live in dialects/<db_engine>.groovy (class LabDialect).
    static Object dialect(Properties p) {
        String engine = p.getProperty('db_engine', 'mysql')
        if (!(engine ==~ /[a-z0-9_]+/)) throw new SQLException("invalid db_engine '${engine}'", '08001')
        synchronized (dialects) {
            if (!dialects.containsKey(engine)) {
                File f = new File(p.getProperty('lab_script_dir', 'jmeter/scripts'), "dialects/${engine}.groovy")
                if (!f.isFile()) throw new SQLException("no JMeter dialect for engine '${engine}': ${f}", '08001')
                dialects[engine] = new GroovyClassLoader(LabDb.class.classLoader).parseClass(f).newInstance()
            }
            return dialects[engine]
        }
    }

    // endpoint_mode: single | writable | first-available
    static Map connect(Properties p, boolean needWrite) {
        List<String> endpoints = p.getProperty('endpoints', '').split(',').collect { it.trim() }.findAll { it }
        if (!endpoints) throw new SQLException('JMeter property endpoints is empty', '08001')
        String mode = p.getProperty('endpoint_mode', 'single')
        String db = p.getProperty('database', 'halab')
        String user = p.getProperty('db_user', 'bench')
        String pass = p.getProperty('db_password', '')
        def D = dialect(p)
        SQLException last = null
        List<String> order = endpoints
        if (mode != 'single' && endpoints.size() > 1) {
            int start = Math.abs((Thread.currentThread().getId() as int)) % endpoints.size()
            order = endpoints.drop(start) + endpoints.take(start)
        }
        for (String ep : (mode == 'single' ? [endpoints[0]] : order)) {
            Connection c = null
            try {
                c = DriverManager.getConnection(D.url(ep, db, p), user, pass)
                Statement st = c.createStatement()
                ResultSet rs = st.executeQuery(D.roQuery())
                rs.next()
                boolean ro = rs.getInt(1) == 1
                String host = rs.getString(2)
                rs.close(); st.close()
                if (needWrite && ro && mode == 'writable') { c.close(); continue }
                c.setAutoCommit(false)
                return [conn: c, servedBy: host, endpoint: ep]
            } catch (SQLException e) {
                last = e
                try { c?.close() } catch (ignored) {}
            }
        }
        throw last ?: new SQLException("no writable endpoint among ${endpoints}", '08001')
    }

    static String classify(Throwable t, Object D) {
        Throwable e = t
        while (e != null) {
            if (e instanceof SQLTimeoutException || e instanceof java.net.SocketTimeoutException) return 'timeout'
            if (e instanceof SQLException) {
                int code = ((SQLException) e).getErrorCode()
                String state = ((SQLException) e).getSQLState() ?: ''
                String cls = D.classifySql(code, state)
                if (cls) return cls
                if (state.startsWith('08')) return 'connection'
            }
            if (e instanceof java.net.ConnectException || e instanceof java.io.EOFException) return 'connection'
            e = e.getCause()
        }
        return 'other'
    }

    static boolean dropsConnection(String cls) {
        return cls in ['connection', 'timeout', 'read_only', 'not_ready', 'commit_unknown', 'other']
    }
}
