package com.froriep.armoryfit;

import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.PixelFormat;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.provider.Settings;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * Floating top-priority pill overlay shown while Armory Fit is minimized and a rest timer is running.
 * Requires SYSTEM_ALERT_WINDOW ("Display over other apps"); no-ops gracefully if not granted.
 */
final class RestOverlay {
    interface Actions {
        void onAdjust(int deltaSec);
        void onSkip();
    }

    private final Context ctx;
    private final WindowManager wm;
    private final Actions actions;
    private View root;
    private GradientDrawable bg;
    private TextView badge;
    private TextView timeView;
    private TextView labelView;

    RestOverlay(Context ctx, Actions actions) {
        this.ctx = ctx.getApplicationContext();
        this.wm = (WindowManager) this.ctx.getSystemService(Context.WINDOW_SERVICE);
        this.actions = actions;
    }

    static boolean canDraw(Context c) {
        return Build.VERSION.SDK_INT < Build.VERSION_CODES.M || Settings.canDrawOverlays(c);
    }

    void showOrUpdate(String label, long remainingMs, boolean fired) {
        if (wm == null || !canDraw(ctx)) return;
        if (root == null) attach();
        if (root == null) return;

        int accent = fired ? 0xFFEF4444 : 0xFF16A34A;
        bg.setStroke(dp(1), accent);
        badge.setText(fired ? "GO" : "REST");
        badge.setTextColor(accent);

        long sec = fired ? Math.max(0L, (-remainingMs) / 1000L) : Math.max(0L, (remainingMs + 999L) / 1000L);
        String t = (fired ? "+" : "") + (sec / 60) + ":" + (sec % 60 < 10 ? "0" : "") + (sec % 60);
        timeView.setText(t);
        timeView.setTextColor(fired ? 0xFFFCA5A5 : Color.WHITE);

        String shortLab = label == null ? "" : label.trim();
        if (shortLab.length() > 18) shortLab = shortLab.substring(0, 17) + "…";
        labelView.setText(shortLab);
        labelView.setVisibility(shortLab.isEmpty() ? View.GONE : View.VISIBLE);
    }

    void hide() {
        if (root != null && wm != null) {
            try {
                wm.removeViewImmediate(root);
            } catch (Exception ignored) {
            }
        }
        root = null;
        bg = null;
        badge = null;
        timeView = null;
        labelView = null;
    }

    private void attach() {
        LinearLayout bar = new LinearLayout(ctx);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(12), dp(7), dp(8), dp(7));

        bg = new GradientDrawable();
        bg.setColor(0xF00D1117);
        bg.setCornerRadius(dp(18));
        bg.setStroke(dp(1), 0xFF16A34A);
        bar.setBackground(bg);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) bar.setElevation(dp(8));

        LinearLayout info = new LinearLayout(ctx);
        info.setOrientation(LinearLayout.HORIZONTAL);
        info.setGravity(Gravity.CENTER_VERTICAL);
        info.setOnClickListener(v -> {
            Intent open = new Intent(ctx, MainActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
            try {
                ctx.startActivity(open);
            } catch (Exception ignored) {
            }
        });

        badge = new TextView(ctx);
        badge.setTextSize(TypedValue.COMPLEX_UNIT_SP, 11);
        badge.setTypeface(Typeface.DEFAULT_BOLD);
        badge.setPadding(0, 0, dp(6), 0);
        info.addView(badge);

        timeView = new TextView(ctx);
        timeView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 16);
        timeView.setTypeface(Typeface.MONOSPACE, Typeface.BOLD);
        timeView.setTextColor(Color.WHITE);
        timeView.setPadding(0, 0, dp(8), 0);
        info.addView(timeView);

        labelView = new TextView(ctx);
        labelView.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        labelView.setTextColor(0xFF94A3B8);
        labelView.setPadding(0, 0, dp(8), 0);
        info.addView(labelView);

        bar.addView(info);
        bar.addView(pillBtn("−15", 0xFF1E293B, Color.WHITE, () -> actions.onAdjust(-15)));
        bar.addView(pillBtn("+15", 0xFF1E293B, Color.WHITE, () -> actions.onAdjust(15)));
        bar.addView(pillBtn("Skip", 0xFF334155, Color.WHITE, actions::onSkip));

        int type = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
                : WindowManager.LayoutParams.TYPE_PHONE;
        WindowManager.LayoutParams lp = new WindowManager.LayoutParams(
                WindowManager.LayoutParams.WRAP_CONTENT,
                WindowManager.LayoutParams.WRAP_CONTENT,
                type,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE | WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                PixelFormat.TRANSLUCENT);
        lp.gravity = Gravity.TOP | Gravity.CENTER_HORIZONTAL;
        lp.y = dp(28);
        try {
            wm.addView(bar, lp);
            root = bar;
        } catch (Exception e) {
            root = null;
        }
    }

    private View pillBtn(String text, int fill, int color, Runnable onTap) {
        TextView tv = new TextView(ctx);
        tv.setText(text);
        tv.setTextSize(TypedValue.COMPLEX_UNIT_SP, 12);
        tv.setTypeface(Typeface.DEFAULT_BOLD);
        tv.setTextColor(color);
        tv.setPadding(dp(9), dp(5), dp(9), dp(5));
        GradientDrawable d = new GradientDrawable();
        d.setColor(fill);
        d.setCornerRadius(dp(12));
        tv.setBackground(d);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.leftMargin = dp(4);
        tv.setLayoutParams(lp);
        tv.setOnClickListener(v -> onTap.run());
        return tv;
    }

    private int dp(int v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, ctx.getResources().getDisplayMetrics()));
    }
}
