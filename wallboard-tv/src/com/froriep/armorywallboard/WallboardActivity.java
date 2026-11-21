package com.froriep.armorywallboard;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

/**
 * Fullscreen Fire TV kiosk WebView activity for the Armory Fit gym wallboard.
 * Handles auto-retry on network disconnects, keeps the display awake while visible,
 * and reloads cleanly on incoming VIEW intents without leaking tabs.
 */
public class WallboardActivity extends Activity {
    private static final String DEFAULT_URL = "http://192.168.1.152:8766/wallboard";
    private static final long RETRY_DELAY_MS = 3000L;

    private final Handler handler = new Handler(Looper.getMainLooper());
    private WebView webView;
    private String currentUrl = DEFAULT_URL;
    private boolean retryScheduled = false;

    private final Runnable retryTask = new Runnable() {
        @Override
        public void run() {
            retryScheduled = false;
            if (webView != null && !isFinishing()) {
                webView.loadUrl(currentUrl);
            }
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        getWindow().addFlags(
                WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                        | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                        | WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                        | WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD);

        applyImmersiveMode();

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.parseColor("#070b12"));

        webView = new WebView(this);
        configureWebView(webView);
        root.addView(webView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);

        currentUrl = extractUrlFromIntent(getIntent(), DEFAULT_URL);
        webView.loadUrl(currentUrl);
    }

    private void configureWebView(WebView wv) {
        wv.setBackgroundColor(Color.parseColor("#070b12"));
        wv.setVerticalScrollBarEnabled(false);
        wv.setHorizontalScrollBarEnabled(false);
        wv.setFocusable(false);

        WebSettings s = wv.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setUseWideViewPort(true);
        s.setLoadWithOverviewMode(true);
        s.setTextZoom(100);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            s.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        }

        wv.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                super.onPageFinished(view, url);
                if (url != null && (url.startsWith("http://") || url.startsWith("https://"))) {
                    cancelRetry();
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    showReconnectingAndRetry();
                }
            }

            @Override
            @SuppressWarnings("deprecation")
            public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
                if (failingUrl == null || failingUrl.equals(currentUrl)) {
                    showReconnectingAndRetry();
                }
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse errorResponse) {
                if (request != null && request.isForMainFrame()
                        && errorResponse != null && errorResponse.getStatusCode() >= 500) {
                    showReconnectingAndRetry();
                }
            }

            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                showReconnectingAndRetry();
                return true;
            }
        });
    }

    private void showReconnectingAndRetry() {
        if (webView == null || isFinishing()) return;
        String html = "<!doctype html><html><body style=\"margin:0;height:100vh;display:flex;"
                + "align-items:center;justify-content:center;background:#070b12;color:#8fa2bf;"
                + "font-family:sans-serif;font-size:28px;font-weight:700;\">"
                + "Armory Wallboard &mdash; Reconnecting&hellip;</body></html>";
        webView.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
        scheduleRetry();
    }

    private void scheduleRetry() {
        if (retryScheduled) return;
        retryScheduled = true;
        handler.postDelayed(retryTask, RETRY_DELAY_MS);
    }

    private void cancelRetry() {
        retryScheduled = false;
        handler.removeCallbacks(retryTask);
    }

    private static String extractUrlFromIntent(Intent intent, String fallback) {
        if (intent == null) return fallback;
        Uri data = intent.getData();
        if (data == null) return fallback;
        String scheme = data.getScheme();
        if ("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme)) {
            return data.toString();
        }
        return fallback;
    }

    private void applyImmersiveMode() {
        View decor = getWindow().getDecorView();
        decor.setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        currentUrl = extractUrlFromIntent(intent, currentUrl);
        applyImmersiveMode();
        cancelRetry();
        if (webView != null) {
            webView.loadUrl(currentUrl);
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        applyImmersiveMode();
        if (webView != null) {
            webView.onResume();
        }
    }

    @Override
    protected void onPause() {
        if (webView != null) {
            webView.onPause();
        }
        super.onPause();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            applyImmersiveMode();
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        if ((keyCode == KeyEvent.KEYCODE_MENU || keyCode == KeyEvent.KEYCODE_MEDIA_PLAY_PAUSE)
                && webView != null) {
            cancelRetry();
            webView.loadUrl(currentUrl);
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    protected void onDestroy() {
        cancelRetry();
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
