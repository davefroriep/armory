package com.froriep.armoryfit;

import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.OpenableColumns;

import java.io.ByteArrayOutputStream;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.URI;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Loopback-only HTTP byte-range server and proxy for streaming Android `content://` DCIM videos
 * and LAN `http://` OptiPlex clips inside the WebView `<video>` element without Mixed Content blocks
 * and with smooth byte-range scrubbing.
 */
final class LocalMediaServer {
    private static final int MAX_HEADER_BYTES = 16_384;

    private static final class Entry {
        final Uri uri;
        final String mime;

        Entry(Uri uri, String mime) {
            this.uri = uri;
            this.mime = mime;
        }
    }

    private static Context appContext;
    private static ServerSocket serverSocket;
    private static Thread acceptThread;
    private static ExecutorService connExec;
    private static volatile boolean running = false;
    private static volatile int boundPort = 0;

    private static final ConcurrentHashMap<String, Entry> tokens = new ConcurrentHashMap<>();
    private static final ConcurrentHashMap<String, String> uriToToken = new ConcurrentHashMap<>();

    private LocalMediaServer() {}

    static synchronized String register(Context c, Uri uri, String mime) throws IOException {
        if (appContext == null && c != null) {
            appContext = c.getApplicationContext();
        }
        ensureStarted();
        String key = uri.toString();
        String tok = uriToToken.get(key);
        if (tok == null || !tokens.containsKey(tok)) {
            tok = UUID.randomUUID().toString().replace("-", "");
            tokens.put(tok, new Entry(uri, (mime != null && !mime.isEmpty()) ? mime : "video/mp4"));
            uriToToken.put(key, tok);
        }
        return "http://127.0.0.1:" + boundPort + "/media/" + tok + ".mp4";
    }

    private static synchronized void ensureStarted() throws IOException {
        if (running && serverSocket != null && !serverSocket.isClosed() && boundPort > 0) return;
        stop();
        ServerSocket ss = new ServerSocket();
        ss.setReuseAddress(true);
        ss.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 0));
        serverSocket = ss;
        boundPort = ss.getLocalPort();
        running = true;
        connExec = Executors.newCachedThreadPool();
        acceptThread = new Thread(() -> acceptLoop(ss), "ArmoryLocalMediaAccept");
        acceptThread.setDaemon(true);
        acceptThread.start();
    }

    static synchronized void stop() {
        running = false;
        boundPort = 0;
        if (serverSocket != null) {
            try { serverSocket.close(); } catch (IOException ignored) {}
            serverSocket = null;
        }
        if (connExec != null) {
            connExec.shutdownNow();
            connExec = null;
        }
        if (acceptThread != null) {
            acceptThread.interrupt();
            acceptThread = null;
        }
        tokens.clear();
        uriToToken.clear();
    }

    private static void acceptLoop(ServerSocket ss) {
        while (running && !ss.isClosed()) {
            try {
                Socket sock = ss.accept();
                ExecutorService ex = connExec;
                if (ex != null && !ex.isShutdown()) {
                    ex.execute(() -> handleClient(sock));
                } else {
                    sock.close();
                }
            } catch (IOException ignored) {
                if (!running) break;
            }
        }
    }

    private static void handleClient(Socket sock) {
        try {
            sock.setSoTimeout(15_000);
            sock.setTcpNoDelay(true);
            InputStream in = sock.getInputStream();
            OutputStream out = sock.getOutputStream();

            String headerBlock = readHeaders(in);
            if (headerBlock == null) return;
            String[] lines = headerBlock.split("\r\n");
            if (lines.length == 0) return;

            String[] req = lines[0].split(" ");
            if (req.length < 2) {
                sendEmpty(out, 400, "Bad Request", null);
                return;
            }
            String method = req[0].toUpperCase();
            String rawPath = req[1];
            int q = rawPath.indexOf('?');
            String path = q >= 0 ? rawPath.substring(0, q) : rawPath;

            if ("OPTIONS".equals(method)) {
                sendEmpty(out, 204, "No Content", "Access-Control-Allow-Methods: GET, HEAD, OPTIONS\r\nAccess-Control-Allow-Headers: Range, Content-Type\r\n");
                return;
            }
            if (!"GET".equals(method) && !"HEAD".equals(method)) {
                sendEmpty(out, 405, "Method Not Allowed", null);
                return;
            }
            if (!path.startsWith("/media/")) {
                sendEmpty(out, 404, "Not Found", null);
                return;
            }
            String tok = path.substring("/media/".length());
            if (tok.endsWith(".mp4")) tok = tok.substring(0, tok.length() - 4);
            Entry entry = tokens.get(tok);
            Context c = appContext;
            if (entry == null || c == null) {
                sendEmpty(out, 404, "Not Found", null);
                return;
            }

            String rangeHdr = null;
            for (int i = 1; i < lines.length; i++) {
                int colon = lines[i].indexOf(':');
                if (colon > 0 && lines[i].substring(0, colon).trim().equalsIgnoreCase("Range")) {
                    rangeHdr = lines[i].substring(colon + 1).trim();
                    break;
                }
            }

            String scheme = entry.uri.getScheme();
            if ("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme)) {
                proxyHttp(entry.uri.toString(), entry.mime, method, rangeHdr, out);
                return;
            }

            try (ParcelFileDescriptor pfd = c.getContentResolver().openFileDescriptor(entry.uri, "r")) {
                if (pfd == null) {
                    sendEmpty(out, 404, "Not Found", null);
                    return;
                }
                long total = pfd.getStatSize();
                if (total <= 0) total = querySize(c, entry.uri);

                try (FileInputStream fis = new FileInputStream(pfd.getFileDescriptor())) {
                    if (total <= 0) {
                        try { total = fis.getChannel().size(); } catch (IOException ignored) {}
                    }
                    if (total <= 0) {
                        sendEmpty(out, 404, "Not Found", null);
                        return;
                    }

                    long start = 0;
                    long end = total - 1;
                    boolean partial = false;
                    if (rangeHdr != null && rangeHdr.startsWith("bytes=")) {
                        String spec = rangeHdr.substring("bytes=".length()).split(",")[0].trim();
                        int dash = spec.indexOf('-');
                        if (dash >= 0) {
                            String sPart = spec.substring(0, dash).trim();
                            String ePart = spec.substring(dash + 1).trim();
                            if (sPart.isEmpty() && !ePart.isEmpty()) {
                                long suffix = Long.parseLong(ePart);
                                start = Math.max(0, total - suffix);
                                end = total - 1;
                                partial = true;
                            } else if (!sPart.isEmpty()) {
                                start = Long.parseLong(sPart);
                                end = ePart.isEmpty() ? total - 1 : Math.min(total - 1, Long.parseLong(ePart));
                                partial = true;
                            }
                        }
                    }

                    if (start < 0 || start >= total || end < start) {
                        sendEmpty(out, 416, "Range Not Satisfiable", "Content-Range: bytes */" + total + "\r\n");
                        return;
                    }

                    long len = end - start + 1;
                    StringBuilder hdr = new StringBuilder();
                    hdr.append(partial ? "HTTP/1.1 206 Partial Content\r\n" : "HTTP/1.1 200 OK\r\n");
                    hdr.append("Content-Type: ").append(entry.mime).append("\r\n");
                    hdr.append("Accept-Ranges: bytes\r\n");
                    hdr.append("Content-Length: ").append(len).append("\r\n");
                    if (partial) {
                        hdr.append("Content-Range: bytes ").append(start).append('-').append(end).append('/').append(total).append("\r\n");
                    }
                    hdr.append("Cache-Control: no-store\r\n");
                    hdr.append("Access-Control-Allow-Origin: *\r\n");
                    hdr.append("Connection: close\r\n\r\n");
                    out.write(hdr.toString().getBytes(StandardCharsets.US_ASCII));

                    if ("GET".equals(method)) {
                        fis.getChannel().position(start);
                        byte[] buf = new byte[65_536];
                        long rem = len;
                        while (rem > 0) {
                            int n = fis.read(buf, 0, (int) Math.min(buf.length, rem));
                            if (n < 0) break;
                            out.write(buf, 0, n);
                            rem -= n;
                        }
                    }
                    out.flush();
                }
            }
        } catch (Exception ignored) {
        } finally {
            try { sock.close(); } catch (IOException ignored) {}
        }
    }

    private static void proxyHttp(String targetUrl, String fallbackMime, String method, String rangeHdr, OutputStream out) throws IOException {
        HttpURLConnection conn = null;
        try {
            URL url = URI.create(targetUrl).toURL();
            conn = (HttpURLConnection) url.openConnection();
            conn.setInstanceFollowRedirects(true);
            conn.setConnectTimeout(4_000);
            conn.setReadTimeout(15_000);
            conn.setRequestMethod(method);
            if (rangeHdr != null && !rangeHdr.isEmpty()) {
                conn.setRequestProperty("Range", rangeHdr);
            }
            int code = conn.getResponseCode();
            String msg = conn.getResponseMessage();
            if (msg == null || msg.isEmpty()) msg = (code == 206 ? "Partial Content" : "OK");
            String ctype = conn.getContentType();
            if (ctype == null || ctype.isEmpty()) ctype = fallbackMime;
            String acceptRanges = conn.getHeaderField("Accept-Ranges");
            if (acceptRanges == null || acceptRanges.isEmpty()) acceptRanges = "bytes";
            String contentRange = conn.getHeaderField("Content-Range");
            long contentLen = conn.getContentLengthLong();

            StringBuilder hdr = new StringBuilder();
            hdr.append("HTTP/1.1 ").append(code).append(' ').append(msg).append("\r\n");
            hdr.append("Content-Type: ").append(ctype).append("\r\n");
            hdr.append("Accept-Ranges: ").append(acceptRanges).append("\r\n");
            if (contentLen >= 0) {
                hdr.append("Content-Length: ").append(contentLen).append("\r\n");
            }
            if (contentRange != null && !contentRange.isEmpty()) {
                hdr.append("Content-Range: ").append(contentRange).append("\r\n");
            }
            hdr.append("Cache-Control: no-store\r\n");
            hdr.append("Access-Control-Allow-Origin: *\r\n");
            hdr.append("Connection: close\r\n\r\n");
            out.write(hdr.toString().getBytes(StandardCharsets.US_ASCII));

            if ("GET".equals(method)) {
                InputStream bodyIn = (code >= 200 && code < 400) ? conn.getInputStream() : conn.getErrorStream();
                if (bodyIn != null) {
                    try (InputStream is = bodyIn) {
                        byte[] buf = new byte[65_536];
                        int n;
                        while ((n = is.read(buf)) >= 0) {
                            out.write(buf, 0, n);
                        }
                    }
                }
            }
            out.flush();
        } catch (IOException e) {
            sendEmpty(out, 502, "Bad Gateway", null);
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    private static long querySize(Context c, Uri uri) {
        try (Cursor cur = c.getContentResolver().query(uri, new String[]{OpenableColumns.SIZE}, null, null, null)) {
            if (cur != null && cur.moveToFirst()) {
                int idx = cur.getColumnIndex(OpenableColumns.SIZE);
                if (idx >= 0) return cur.getLong(idx);
            }
        } catch (Exception ignored) {}
        return 0;
    }

    private static String readHeaders(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream(512);
        int state = 0;
        while (buf.size() < MAX_HEADER_BYTES) {
            int b = in.read();
            if (b < 0) return null;
            buf.write(b);
            if (state == 0 && b == '\r') state = 1;
            else if (state == 1 && b == '\n') state = 2;
            else if (state == 2 && b == '\r') state = 3;
            else if (state == 3 && b == '\n') {
                return buf.toString(StandardCharsets.ISO_8859_1.name());
            } else {
                state = (b == '\r') ? 1 : 0;
            }
        }
        return null;
    }

    private static void sendEmpty(OutputStream out, int code, String reason, String extraHdrs) throws IOException {
        StringBuilder sb = new StringBuilder();
        sb.append("HTTP/1.1 ").append(code).append(' ').append(reason).append("\r\n");
        sb.append("Access-Control-Allow-Origin: *\r\n");
        sb.append("Connection: close\r\n");
        if (extraHdrs != null) sb.append(extraHdrs);
        sb.append("Content-Length: 0\r\n\r\n");
        out.write(sb.toString().getBytes(StandardCharsets.US_ASCII));
        out.flush();
    }
}
