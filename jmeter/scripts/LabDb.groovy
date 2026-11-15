// Shared JDBC helpers, loaded by transaction.groovy and prepare.groovy via GroovyShell.
import java.sql.*

class LabDb {
    static String url(String hostPort, String db, Properties p) {
        String connectMs = p.getProperty('connect_timeout_ms', '3000')
        String socketMs = p.getProperty('socket_timeout_ms', '15000')
        return "jdbc:mysql://${hostPort}/${db}?sslMode=PREFERRED&allowPublicKeyRetrieval=true" +
               "&connectTimeout=${connectMs}&socketTimeout=${socketMs}&tcpKeepAlive=true" +
               "&rewriteBatchedStatements=true&createDatabaseIfNotExist=false&useServerPrepStmts=false"
    }

    // endpoint_mode: single | writable | first-available
    static Map connect(Properties p, boolean needWrite) {
        List<String> endpoints = p.getProperty('endpoints', '').split(',').collect { it.trim() }.findAll { it }
        if (!endpoints) throw new SQLException('JMeter property endpoints is empty', '08001')
        String mode = p.getProperty('endpoint_mode', 'single')
        String db = p.getProperty('database', 'halab')
        String user = p.getProperty('db_user', 'bench')
        String pass = p.getProperty('db_password', '')
        SQLException last = null
        List<String> order = endpoints
        if (mode != 'single' && endpoints.size() > 1) {
            int start = Math.abs((Thread.currentThread().getId() as int)) % endpoints.size()
            order = endpoints.drop(start) + endpoints.take(start)
        }
        for (String ep : (mode == 'single' ? [endpoints[0]] : order)) {
            Connection c = null
            try {
                c = DriverManager.getConnection(url(ep, db, p), user, pass)
                Statement st = c.createStatement()
                ResultSet rs = st.executeQuery('SELECT @@global.read_only OR @@global.super_read_only, @@report_host, @@hostname')
                rs.next()
                boolean ro = rs.getInt(1) == 1
                String host = rs.getString(2) ?: rs.getString(3)
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

    static String classify(Throwable t) {
        Throwable e = t
        while (e != null) {
            if (e instanceof SQLTimeoutException || e instanceof java.net.SocketTimeoutException) return 'timeout'
            if (e instanceof SQLException) {
                int code = ((SQLException) e).getErrorCode()
                String state = ((SQLException) e).getSQLState() ?: ''
                if (code == 1290 || code == 1836) return 'read_only'
                if (code == 1213) return 'deadlock'
                if (code == 1205) return 'lock_timeout'
                if (code == 1062) return 'duplicate'
                if (code == 3100 || code == 3101) return 'conflict'
                if (code == 1047 || code == 3796) return 'not_ready'
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
