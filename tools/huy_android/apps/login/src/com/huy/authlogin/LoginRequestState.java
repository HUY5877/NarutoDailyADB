package com.huy.authlogin;

/** UI-thread-only gate. A response may restore credentials only for its active input and session. */
public final class LoginRequestState<T> {
    private long generation;
    private boolean foreground;
    private String revision;
    private T value;

    public void resume() { invalidate(); foreground = true; }

    public void stop() { foreground = false; invalidate(); }

    public void invalidate() { generation++; revision = null; value = null; }

    public long begin(String sessionRevision) {
        invalidate();
        if (!foreground || sessionRevision == null) throw new IllegalStateException("Inactive login request");
        revision = sessionRevision;
        return generation;
    }

    public boolean isCurrent(long attempt, String currentRevision) {
        return foreground && generation == attempt && revision != null && revision.equals(currentRevision);
    }

    /** Derive button busy state from the request gate so lifecycle invalidation also enables retry. */
    public boolean isPending() { return foreground && revision != null && value == null; }

    public boolean accept(long attempt, String currentRevision, T result) {
        if (!isCurrent(attempt, currentRevision) || result == null) return false;
        value = result;
        return true;
    }

    public T value(String currentRevision) {
        if (!foreground || revision == null || !revision.equals(currentRevision)) {
            invalidate();
            return null;
        }
        return value;
    }
}
