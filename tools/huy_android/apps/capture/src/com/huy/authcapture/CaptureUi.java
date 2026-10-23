package com.huy.authcapture;

import android.app.Activity;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

final class CaptureUi {
    static final int BG = Color.rgb(244, 244, 244);
    static final int INK = Color.rgb(34, 34, 34), MUTED = Color.rgb(119, 119, 119);
    static final int ACCENT = Color.rgb(53, 104, 168), LINE = Color.rgb(221, 221, 221);
    static int dp(Activity a, int n) { return (int) (n * a.getResources().getDisplayMetrics().density + .5f); }

    static LinearLayout page(Activity a, String title, String subtitle) {
        a.getWindow().setStatusBarColor(Color.WHITE);
        a.getWindow().setNavigationBarColor(BG);
        a.getWindow().getDecorView().setSystemUiVisibility(View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
        LinearLayout root = new LinearLayout(a);
        root.setOrientation(LinearLayout.VERTICAL); root.setBackgroundColor(BG);
        LinearLayout toolbar = new LinearLayout(a);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(dp(a,20),0,dp(a,20),0);
        toolbar.setBackgroundColor(Color.WHITE);
        TextView heading = text(a,title,20,INK);
        heading.setAccessibilityHeading(true);
        toolbar.addView(heading,new LinearLayout.LayoutParams(0,-2,1));
        TextView mark = text(a,"HUY",12,MUTED);
        mark.setTypeface(Typeface.MONOSPACE);
        toolbar.addView(mark);
        root.addView(toolbar,new LinearLayout.LayoutParams(-1,dp(a,56)));
        View line = new View(a); line.setBackgroundColor(LINE);
        root.addView(line,new LinearLayout.LayoutParams(-1,dp(a,1)));
        ScrollView scroll = new ScrollView(a);
        root.addView(scroll,new LinearLayout.LayoutParams(-1,0,1));
        LinearLayout center = new LinearLayout(a);
        center.setGravity(Gravity.TOP | Gravity.CENTER_HORIZONTAL);
        center.setPadding(dp(a,24),dp(a,24),dp(a,24),dp(a,24));
        scroll.addView(center);
        LinearLayout body = new LinearLayout(a);
        body.setOrientation(LinearLayout.VERTICAL);
        int width = Math.min(dp(a,480),a.getResources().getDisplayMetrics().widthPixels-dp(a,48));
        center.addView(body,new LinearLayout.LayoutParams(width,-2));
        body.addView(text(a,subtitle,13,MUTED));
        a.setContentView(root);
        return body;
    }

    static TextView text(Activity a, String value, int size, int color) {
        TextView t = new TextView(a); t.setText(value); t.setTextSize(size); t.setTextColor(color);
        t.setLineSpacing(dp(a,3),1); return t;
    }

    static TextView status(Activity a, LinearLayout parent) {
        TextView t = text(a,"",15,INK);
        t.setPadding(dp(a,14),dp(a,14),dp(a,14),dp(a,14));
        GradientDrawable bg = new GradientDrawable(); bg.setColor(Color.WHITE); bg.setStroke(dp(a,1),LINE); t.setBackground(bg);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1,-2); lp.setMargins(0,dp(a,12),0,dp(a,20));
        parent.addView(t,lp); return t;
    }

    static Button button(Activity a, LinearLayout parent, String title, boolean primary, View.OnClickListener action) {
        Button b = new Button(a); b.setText(title); b.setAllCaps(false); b.setTextSize(15);
        b.setTextColor(primary ? Color.WHITE : INK);
        b.setMinHeight(0); b.setMinimumHeight(0); b.setElevation(0);
        GradientDrawable bg = new GradientDrawable(); bg.setColor(primary ? ACCENT : Color.WHITE);
        bg.setCornerRadius(dp(a,3));
        if (!primary) bg.setStroke(dp(a,1),LINE);
        b.setBackground(new RippleDrawable(ColorStateList.valueOf(0x18000000),bg,null));
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(-1,dp(a,44)); lp.setMargins(0,0,0,dp(a,12));
        parent.addView(b,lp); b.setOnClickListener(action); return b;
    }
}
