package com.froriep.armoryfit;

import android.content.Context;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.util.Log;

import com.getcapacitor.JSObject;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.NetworkInterface;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.Collections;
import java.util.Enumeration;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import java.util.regex.Pattern;

/**
 * Lightweight local Wi-Fi HTTP + SSE server embedded inside the Pixel APK.
 * Serves the self-contained 10-foot Smart TV / monitor wallboard (`GET /`),
 * JSON state (`GET /state`), real-time SSE updates (`GET /events`),
 * and local LAN voice transcript injection (`POST /api/voice`).
 */
final class WallboardServer {
    private static final String TAG = "ArmoryWallboard";
    private static final int MAX_HEADER_BYTES = 16_384;
    private static final int MAX_BODY_BYTES = 8_192;

    private static ServerSocket serverSocket;
    private static Thread acceptThread;
    private static ExecutorService connExec;
    private static ExecutorService sseExec;
    private static ScheduledExecutorService pingExec;
    private static WifiManager.WifiLock wifiLock;

    private static final CopyOnWriteArrayList<ClientStream> sseClients = new CopyOnWriteArrayList<>();
    private static volatile boolean running = false;
    private static volatile int boundPort = 8765;
    private static volatile String htmlPage = "<!doctype html><html><body>Armory Fit Wallboard starting...</body></html>";
    private static volatile String latestStateJson = "{\"mode\":\"standby\"}";

    private static final class ClientStream {
        final Socket socket;
        final OutputStream out;
        final AtomicReference<byte[]> pending = new AtomicReference<>();
        final AtomicBoolean draining = new AtomicBoolean(false);

        ClientStream(Socket socket, OutputStream out) {
            this.socket = socket;
            this.out = out;
        }
    }

    private static final Pattern LAN_ORIGIN_RE = Pattern.compile(
            "^https?://(?:localhost|127\\.0\\.0\\.1|10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}|192\\.168\\.\\d{1,3}\\.\\d{1,3}|172\\.(?:1[6-9]|2\\d|3[01])\\.\\d{1,3}\\.\\d{1,3})(?::\\d{1,5})?$",
            Pattern.CASE_INSENSITIVE);

    private static boolean isLanOrigin(String origin) {
        return origin != null && LAN_ORIGIN_RE.matcher(origin.trim()).matches();
    }

    private WallboardServer() {}

    static synchronized JSObject start(Context c, int port, String html) {
        if (html != null && !html.isEmpty()) {
            htmlPage = html;
        }
        int wantPort = (port >= 1024 && port <= 65535) ? port : 8765;
        if (running && serverSocket != null && !serverSocket.isClosed() && boundPort == wantPort) {
            return status();
        }
        stop();

        try {
            Context app = c.getApplicationContext();
            WifiManager wm = (WifiManager) app.getSystemService(Context.WIFI_SERVICE);
            if (wm != null) {
                int mode = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
                        ? WifiManager.WIFI_MODE_FULL_LOW_LATENCY
                        : WifiManager.WIFI_MODE_FULL_HIGH_PERF;
                wifiLock = wm.createWifiLock(mode, "ArmoryFit:Wallboard");
                wifiLock.setReferenceCounted(false);
                wifiLock.acquire();
            }
        } catch (Exception e) {
            Log.w(TAG, "WifiLock unavailable", e);
        }

        try {
            ServerSocket ss = new ServerSocket();
            ss.setReuseAddress(true);
            ss.bind(new InetSocketAddress("0.0.0.0", wantPort));
            serverSocket = ss;
            boundPort = ss.getLocalPort();
            running = true;

            connExec = Executors.newCachedThreadPool();
            sseExec = Executors.newFixedThreadPool(8);
            pingExec = Executors.newSingleThreadScheduledExecutor();
            pingExec.scheduleWithFixedDelay(() -> {
                if (sseClients.isEmpty() || sseExec == null) return;
                writeAll(": ping\n\n".getBytes(StandardCharsets.UTF_8), false);
            }, 15, 15, TimeUnit.SECONDS);

            acceptThread = new Thread(() -> acceptLoop(ss), "ArmoryWallboardAccept");
            acceptThread.setDaemon(true);
            acceptThread.start();
        } catch (Exception e) {
            Log.e(TAG, "Failed to start WallboardServer on port " + wantPort, e);
            stop();
            JSObject err = status();
            err.put("error", e.getMessage());
            return err;
        }
        return status();
    }

    static synchronized void stop() {
        running = false;
        if (serverSocket != null) {
            try { serverSocket.close(); } catch (IOException ignored) {}
            serverSocket = null;
        }
        for (ClientStream cs : sseClients) {
            try { cs.socket.close(); } catch (IOException ignored) {}
        }
        sseClients.clear();
        if (pingExec != null) {
            pingExec.shutdownNow();
            pingExec = null;
        }
        if (sseExec != null) {
            sseExec.shutdownNow();
            sseExec = null;
        }
        if (connExec != null) {
            connExec.shutdownNow();
            connExec = null;
        }
        if (acceptThread != null) {
            acceptThread.interrupt();
            acceptThread = null;
        }
        if (wifiLock != null) {
            try {
                if (wifiLock.isHeld()) wifiLock.release();
            } catch (Exception ignored) {}
            wifiLock = null;
        }
    }

    static int pushState(String json) {
        if (json == null || json.isEmpty()) return sseClients.size();
        String clean = json.replace('\r', ' ').replace('\n', ' ');
        latestStateJson = clean;
        ExecutorService ex = sseExec;
        if (running && ex != null && !sseClients.isEmpty()) {
            byte[] frame = ("data: " + clean + "\n\n").getBytes(StandardCharsets.UTF_8);
            writeAll(frame, true);
        }
        return sseClients.size();
    }

    static JSObject status() {
        String ip = findLanIp();
        JSObject ret = new JSObject();
        ret.put("running", running);
        ret.put("port", boundPort);
        ret.put("ip", ip);
        ret.put("url", "http://" + ip + ":" + boundPort);
        ret.put("clients", sseClients.size());
        return ret;
    }

    static String findLanIp() {
        String fallback = null;
        try {
            Enumeration<NetworkInterface> ifaces = NetworkInterface.getNetworkInterfaces();
            if (ifaces == null) return "127.0.0.1";
            for (NetworkInterface nif : Collections.list(ifaces)) {
                if (!nif.isUp() || nif.isLoopback() || nif.isVirtual()) continue;
                String name = nif.getName() != null ? nif.getName().toLowerCase() : "";
                if (name.startsWith("rmnet") || name.startsWith("ccmni") || name.startsWith("pdp") || name.startsWith("dummy")) {
                    continue;
                }
                Enumeration<InetAddress> addrs = nif.getInetAddresses();
                for (InetAddress addr : Collections.list(addrs)) {
                    if (addr instanceof Inet4Address && addr.isSiteLocalAddress() && !addr.isLoopbackAddress()) {
                        String host = addr.getHostAddress();
                        if (name.startsWith("wlan") || name.startsWith("eth")) return host;
                        if (fallback == null) fallback = host;
                    }
                }
            }
        } catch (Exception ignored) {}
        return fallback != null ? fallback : "127.0.0.1";
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

    private static void writeAll(byte[] payload, boolean isData) {
        ExecutorService ex = sseExec;
        if (ex == null || ex.isShutdown()) return;
        for (ClientStream cs : sseClients) {
            if (isData) {
                cs.pending.set(payload);
            } else {
                cs.pending.compareAndSet(null, payload);
            }
            if (cs.draining.compareAndSet(false, true)) {
                try {
                    ex.execute(() -> drainClient(cs));
                } catch (Exception ignored) {
                    cs.draining.set(false);
                }
            }
        }
    }

    private static void drainClient(ClientStream cs) {
        try {
            while (running && !cs.socket.isClosed() && sseClients.contains(cs)) {
                byte[] frame = cs.pending.getAndSet(null);
                if (frame == null) break;
                synchronized (cs.out) {
                    cs.out.write(frame);
                    cs.out.flush();
                }
            }
        } catch (IOException e) {
            sseClients.remove(cs);
            try { cs.socket.close(); } catch (IOException ignored) {}
        } finally {
            cs.draining.set(false);
            if (running && !cs.socket.isClosed() && sseClients.contains(cs) && cs.pending.get() != null) {
                ExecutorService ex = sseExec;
                if (ex != null && !ex.isShutdown() && cs.draining.compareAndSet(false, true)) {
                    try {
                        ex.execute(() -> drainClient(cs));
                    } catch (Exception ignored) {
                        cs.draining.set(false);
                    }
                }
            }
        }
    }

    private static void handleClient(Socket sock) {
        boolean upgradedSse = false;
        try {
            sock.setSoTimeout(10_000);
            sock.setTcpNoDelay(true);
            InputStream in = sock.getInputStream();
            OutputStream out = sock.getOutputStream();

            String headerBlock = readHeaders(in);
            if (headerBlock == null) return;
            String[] lines = headerBlock.split("\r\n");
            if (lines.length == 0) return;

            String[] req = lines[0].split(" ");
            if (req.length < 2) {
                sendText(out, 400, "Bad Request", "text/plain; charset=utf-8", "Bad Request", null, null);
                return;
            }
            String method = req[0].toUpperCase();
            String rawPath = req[1];
            int q = rawPath.indexOf('?');
            String path = q >= 0 ? rawPath.substring(0, q) : rawPath;

            int contentLength = 0;
            String origin = null;
            for (int i = 1; i < lines.length; i++) {
                int colon = lines[i].indexOf(':');
                if (colon > 0) {
                    String hName = lines[i].substring(0, colon).trim();
                    String hVal = lines[i].substring(colon + 1).trim();
                    if (hName.equalsIgnoreCase("Content-Length")) {
                        try { contentLength = Integer.parseInt(hVal); }
                        catch (NumberFormatException ignored) {}
                    } else if (hName.equalsIgnoreCase("Origin")) {
                        origin = hVal;
                    }
                }
            }

            if ("OPTIONS".equals(method)) {
                if (origin != null && !origin.isEmpty() && !isLanOrigin(origin)) {
                    sendText(out, 403, "Forbidden", "application/json; charset=utf-8", "{\"error\":\"Origin not allowed\"}", null, null);
                    return;
                }
                String corsOrigin = (origin != null && !origin.isEmpty()) ? origin : null;
                sendText(out, 204, "No Content", "text/plain; charset=utf-8", "",
                        "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\nAccess-Control-Allow-Headers: Content-Type\r\n",
                        corsOrigin);
                return;
            }

            if ("GET".equals(method) && ("/".equals(path) || "/index.html".equals(path))) {
                sendText(out, 200, "OK", "text/html; charset=utf-8", htmlPage, null, "*");
                return;
            }

            if ("GET".equals(method) && "/state".equals(path)) {
                sendText(out, 200, "OK", "application/json; charset=utf-8", latestStateJson, null, "*");
                return;
            }

            if ("GET".equals(method) && "/events".equals(path)) {
                sock.setSoTimeout(0);
                String hdr = "HTTP/1.1 200 OK\r\n"
                        + "Content-Type: text/event-stream; charset=utf-8\r\n"
                        + "Cache-Control: no-cache\r\n"
                        + "Connection: keep-alive\r\n"
                        + "Access-Control-Allow-Origin: *\r\n\r\n"
                        + "data: " + latestStateJson + "\n\n";
                synchronized (out) {
                    out.write(hdr.getBytes(StandardCharsets.UTF_8));
                    out.flush();
                }
                ClientStream cs = new ClientStream(sock, out);
                sseClients.add(cs);
                upgradedSse = true;
                while (running && !sock.isClosed() && sseClients.contains(cs)) {
                    int b = in.read();
                    if (b < 0) break;
                }
                sseClients.remove(cs);
                return;
            }

            if ("POST".equals(method) && "/api/voice".equals(path)) {
                InetAddress remote = sock.getInetAddress();
                if (remote == null || (!remote.isLoopbackAddress() && !remote.isSiteLocalAddress())) {
                    sendText(out, 403, "Forbidden", "application/json; charset=utf-8", "{\"error\":\"LAN only\"}", null, null);
                    return;
                }
                if (origin != null && !origin.isEmpty() && !isLanOrigin(origin)) {
                    sendText(out, 403, "Forbidden", "application/json; charset=utf-8", "{\"error\":\"Origin not allowed\"}", null, null);
                    return;
                }
                String corsOrigin = (origin != null && !origin.isEmpty()) ? origin : null;
                int len = Math.max(0, Math.min(MAX_BODY_BYTES, contentLength));
                byte[] bodyBytes = new byte[len];
                int read = 0;
                while (read < len) {
                    int r = in.read(bodyBytes, read, len - read);
                    if (r < 0) break;
                    read += r;
                }
                String body = new String(bodyBytes, 0, read, StandardCharsets.UTF_8);
                String transcript = "";
                try {
                    transcript = new JSONObject(body).optString("transcript", "").trim();
                } catch (Exception ignored) {}
                if (!transcript.isEmpty()) {
                    WorkoutServicePlugin.deliverExternalVoice(transcript, "lan");
                }
                sendText(out, 200, "OK", "application/json; charset=utf-8", "{\"ok\":true}", null, corsOrigin);
                return;
            }

            sendText(out, 404, "Not Found", "text/plain; charset=utf-8", "Not Found", null, null);
        } catch (IOException ignored) {
        } finally {
            if (!upgradedSse) {
                try { sock.close(); } catch (IOException ignored) {}
            }
        }
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

    private static void sendText(OutputStream out, int code, String reason, String mime, String body, String extraHdrs, String corsOrigin) throws IOException {
        byte[] bytes = body != null ? body.getBytes(StandardCharsets.UTF_8) : new byte[0];
        StringBuilder sb = new StringBuilder();
        sb.append("HTTP/1.1 ").append(code).append(' ').append(reason).append("\r\n");
        sb.append("Content-Type: ").append(mime).append("\r\n");
        sb.append("Cache-Control: no-store\r\n");
        if (corsOrigin != null && !corsOrigin.isEmpty()) {
            sb.append("Access-Control-Allow-Origin: ").append(corsOrigin).append("\r\n");
            if (!"*".equals(corsOrigin)) sb.append("Vary: Origin\r\n");
        }
        sb.append("Connection: close\r\n");
        if (extraHdrs != null) sb.append(extraHdrs);
        sb.append("Content-Length: ").append(bytes.length).append("\r\n\r\n");
        out.write(sb.toString().getBytes(StandardCharsets.US_ASCII));
        if (bytes.length > 0) out.write(bytes);
        out.flush();
    }
}
