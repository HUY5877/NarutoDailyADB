package com.huy.authlogin;

/** UI-thread-only gate: each response belongs to its active mode, input and applicable cloud session. */
public final class LoginRequestState<T> {
    public enum Mode { DIRECT, CLOUD }
    private Mode mode = Mode.DIRECT;
    private long generation;
    private boolean foreground;
    private boolean started;
    private String revision;
    private T value;

    public void resume() { invalidate(); foreground = true; }

    public void stop() { foreground = false; invalidate(); }

    public Mode mode() { return mode; }
    public void setMode(Mode next) { if (mode != next) { mode = next; invalidate(); } }
    public void websiteSessionChanged() { if (mode == Mode.CLOUD) invalidate(); }

    public void invalidate() { generation++; started = false; revision = null; value = null; }

    public long begin(String sessionRevision) {
        invalidate();
        if (!foreground || (mode == Mode.CLOUD && sessionRevision == null)) throw new IllegalStateException("Inactive login request");
        started = true;
        revision = sessionRevision;
        return generation;
    }

    public boolean isCurrent(long attempt, String currentRevision) {
        return foreground && started && generation == attempt && sessionMatches(currentRevision);
    }

    /** Derive button busy state from the request gate so lifecycle invalidation also enables retry. */
    public boolean isPending() { return foreground && started && value == null; }

    public boolean accept(long attempt, String currentRevision, T result) {
        if (!isCurrent(attempt, currentRevision) || result == null) return false;
        value = result;
        return true;
    }

    public T value(String currentRevision) {
        if (!foreground || !started || !sessionMatches(currentRevision)) {
            invalidate();
            return null;
        }
        return value;
    }

    private boolean sessionMatches(String currentRevision) {
        return mode == Mode.DIRECT || (revision != null && revision.equals(currentRevision));
    }
}
