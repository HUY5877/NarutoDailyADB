package com.huy.authlogin;

import android.app.Activity;
import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/** Small native layout, shared by this app's two activities. */
public final class HuyUi {
    public static final int BACKGROUND = Color.rgb(244, 244, 244);
    public static final int CARD = Color.WHITE;
    public static final int ACCENT = Color.rgb(53, 104, 168);
    private static final int TEXT = Color.rgb(34, 34, 34);
    private static final int MUTED = Color.rgb(119, 119, 119);
    private static final int BORDER = Color.rgb(221, 221, 221);

    private HuyUi() {}

    public static final class Screen {
        public final Activity activity;
        public final LinearLayout column;
        public final TextView statusTitle;
        public final TextView statusDetail;

        public Screen(Activity activity, String title, String subtitle) {
            this.activity = activity;
            activity.getWindow().addFlags(WindowManager.LayoutParams.FLAG_SECURE);
            activity.getWindow().setStatusBarColor(CARD);
            activity.getWindow().setNavigationBarColor(BACKGROUND);
            activity.getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR | View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);

            LinearLayout root = new LinearLayout(activity);
            root.setOrientation(LinearLayout.VERTICAL);
            root.setBackgroundColor(BACKGROUND);
            LinearLayout toolbar = new LinearLayout(activity);
            toolbar.setGravity(Gravity.CENTER_VERTICAL);
            toolbar.setPadding(dp(activity, 24), 0, dp(activity, 24), 0);
            toolbar.setBackgroundColor(CARD);
            TextView heading = text(activity, title, 20, TEXT, false);
            heading.setAccessibilityHeading(true);
            toolbar.addView(heading, new LinearLayout.LayoutParams(
                    0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
            TextView brand = text(activity, "HUY", 12, MUTED, false);
            brand.setTypeface(Typeface.MONOSPACE);
            toolbar.addView(brand, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT));
            root.addView(toolbar, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, dp(activity, 56)));
            root.addView(divider(activity));

            ScrollView scroll = new ScrollView(activity);
            scroll.setFillViewport(true);
            scroll.setBackgroundColor(BACKGROUND);
            scroll.setClipToPadding(false);
            root.addView(scroll, new LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
            FrameLayout host = new FrameLayout(activity);
            scroll.addView(host, new ScrollView.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
            column = new MaxWidthColumn(activity);
            column.setOrientation(LinearLayout.VERTICAL);
            column.setPadding(dp(activity, 24), dp(activity, 24), dp(activity, 24), dp(activity, 24));
            host.addView(column, new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT,
                    Gravity.TOP | Gravity.CENTER_HORIZONTAL));
            column.addView(text(activity, subtitle, 14, MUTED, false), spacing(activity, 0, 20));

            LinearLayout card = new LinearLayout(activity);
            card.setOrientation(LinearLayout.VERTICAL);
            card.setBackgroundColor(CARD);
            card.addView(divider(activity));
            LinearLayout status = new LinearLayout(activity);
            status.setOrientation(LinearLayout.VERTICAL);
            status.setPadding(dp(activity, 16), dp(activity, 16), dp(activity, 16), dp(activity, 16));
            statusTitle = text(activity, "正在读取授权", 16, TEXT, false);
            statusTitle.setAccessibilityLiveRegion(View.ACCESSIBILITY_LIVE_REGION_POLITE);
            status.addView(statusTitle, spacing(activity, 0, 8));
            statusDetail = text(activity, "请稍候。", 13, MUTED, false);
            status.addView(statusDetail, spacing(activity, 0, 0));
            card.addView(status, spacing(activity, 0, 0));
            card.addView(divider(activity));
            column.addView(card, spacing(activity, 0, 20));
            activity.setContentView(root);
        }

        public Button button(String label, boolean primary, View.OnClickListener listener) {
            Button button = new Button(activity);
            button.setText(label);
            button.setTextSize(16);
            button.setAllCaps(false);
            button.setTypeface(Typeface.create("sans-serif", Typeface.NORMAL));
            button.setTextColor(primary ? Color.WHITE : TEXT);
            button.setMinHeight(0);
            button.setMinimumHeight(0);
            button.setPadding(dp(activity, 16), 0, dp(activity, 16), 0);
            button.setElevation(0);
            button.setStateListAnimator(null);
            GradientDrawable background = shape(activity, primary ? ACCENT : CARD, 3);
            if (!primary) background.setStroke(dp(activity, 1), BORDER);
            button.setBackground(new RippleDrawable(ColorStateList.valueOf(0x223568A8), background, null));
            button.setOnClickListener(listener);
            LinearLayout.LayoutParams parameters = spacing(activity, 0, 10);
            parameters.height = dp(activity, 44);
            column.addView(button, parameters);
            return button;
        }

        public void footnote(String value) {
            TextView note = text(activity, value, 12, MUTED, false);
            column.addView(note, spacing(activity, 10, 0));
        }

        public void show(AuthRepository.Outcome result) {
            show(result.title, result.detail);
        }

        public void show(String title, String detail) {
            statusTitle.setText(title);
            statusTitle.setTextColor(TEXT);
            statusDetail.setText(detail);
        }

        public void busy(String description) {
            statusTitle.setText("正在处理");
            statusTitle.setTextColor(TEXT);
            statusDetail.setText(description);
        }
    }

    public static void enabled(Button button, boolean enabled) {
        button.setEnabled(enabled);
        button.setAlpha(enabled ? 1f : 0.45f);
    }

    public static EditText authorizationInput(Screen screen) {
        EditText input = new EditText(screen.activity);
        input.setHint("粘贴 token 或完整 QQ 回调地址");
        input.setContentDescription("QQ 授权 token 或完整回调地址");
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        input.setImeOptions(EditorInfo.IME_ACTION_DONE | EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING);
        input.setSaveEnabled(false);
        input.setFreezesText(false);
        input.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        screen.column.addView(input, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));
        return input;
    }

    private static TextView text(Context context, String value, int size, int color, boolean bold) {
        TextView text = new TextView(context);
        text.setText(value);
        text.setTextSize(size);
        text.setTextColor(color);
        text.setFontFeatureSettings("kern");
        text.setIncludeFontPadding(false);
        text.setLineSpacing(dp(context, 4), 1.0f);
        if (bold) text.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        return text;
    }

    private static GradientDrawable shape(Context context, int color, int radius) {
        GradientDrawable shape = new GradientDrawable();
        shape.setColor(color);
        shape.setCornerRadius(dp(context, radius));
        return shape;
    }

    private static View divider(Context context) {
        View line = new View(context);
        line.setBackgroundColor(BORDER);
        line.setLayoutParams(new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(context, 1)));
        return line;
    }

    private static LinearLayout.LayoutParams spacing(Context context, int top, int bottom) {
        LinearLayout.LayoutParams parameters = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        parameters.topMargin = dp(context, top);
        parameters.bottomMargin = dp(context, bottom);
        return parameters;
    }

    private static int dp(Context context, int value) {
        return Math.round(value * context.getResources().getDisplayMetrics().density);
    }

    private static final class MaxWidthColumn extends LinearLayout {
        MaxWidthColumn(Context context) {
            super(context);
        }

        @Override protected void onMeasure(int widthMeasureSpec, int heightMeasureSpec) {
            int width = Math.min(MeasureSpec.getSize(widthMeasureSpec), dp(getContext(), 480));
            super.onMeasure(MeasureSpec.makeMeasureSpec(width, MeasureSpec.EXACTLY), heightMeasureSpec);
        }
    }
}
