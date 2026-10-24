package com.huy.authlogin;

/** Exercises the same response acceptance gate used by the game confirmation screen. */
public final class LoginRequestStateTest {
    private static int assertions;

    public static void main(String[] args) {
        directAuthorizationDoesNotNeedWebsiteSession();
        switchingModesRejectsOldCloudResponse();
        LoginRequestState<String> state = new LoginRequestState<>();
        state.setMode(LoginRequestState.Mode.CLOUD);
        state.resume();
        long initial = state.begin("website-a");
        state.invalidate(); // User starts typing before startup's selected download completes.
        check(!state.accept(initial, "website-a", "selected"));
        check(state.value("website-a") == null);
        long first = state.begin("website-a");
        long second = state.begin("website-a");
        check(!state.accept(first, "website-a", "older"));
        check(state.accept(second, "website-a", "newer"));
        check("newer".equals(state.value("website-a")));
        state.invalidate(); // Edit after success must disable confirmation immediately.
        check(state.value("website-a") == null);
        check(!state.accept(second, "website-a", "late"));
        long changed = state.begin("website-a");
        check(!state.accept(changed, "website-b", "wrong-session"));
        check(state.value("website-b") == null);
        long stopped = state.begin("website-a");
        state.stop(); // Also used for cancel/home/destroy.
        check(!state.accept(stopped, "website-a", "after-stop"));
        state.resume();
        check(!state.accept(stopped, "website-a", "after-resume"));
        check(state.value("website-a") == null);
        long fresh = state.begin("website-a");
        check(state.accept(fresh, "website-a", "ready"));
        check(state.value("website-b") == null); // Recheck at explicit confirmation.
        check(state.value("website-a") == null); // Old session cannot revive cached auth.
        resumeWithoutStopAllowsRetry(state);
        System.out.println("LoginRequestStateTest: " + assertions + " assertions passed");
    }

    private static void resumeWithoutStopAllowsRetry(LoginRequestState<String> state) {
        long beforePause = state.begin("website-a");
        check(state.isPending());
        // Android may call onPause then onResume without onStop (for example multi-window).
        state.resume();
        check(!state.isPending()); // The read button must allow retry with the same input.
        check(!state.accept(beforePause, "website-a", "late-after-pause"));
        check(state.value("website-a") == null);
        long retry = state.begin("website-a");
        check(state.isPending());
        check(state.accept(retry, "website-a", "retried"));
        check(!state.isPending());
        check("retried".equals(state.value("website-a")));
    }

    private static void directAuthorizationDoesNotNeedWebsiteSession() {
        LoginRequestState<String> direct = new LoginRequestState<>();
        direct.resume();
        long attempt = direct.begin(null);
        direct.websiteSessionChanged();
        check(direct.accept(attempt, "website-a", "direct"));
        check("direct".equals(direct.value("website-b")));
        direct.websiteSessionChanged();
        check("direct".equals(direct.value(null)));
    }

    private static void switchingModesRejectsOldCloudResponse() {
        LoginRequestState<String> state = new LoginRequestState<>();
        state.resume();
        state.setMode(LoginRequestState.Mode.CLOUD);
        long old = state.begin("website-a");
        state.setMode(LoginRequestState.Mode.DIRECT);
        long direct = state.begin(null);
        check(!state.isCurrent(old, "website-a"));
        check(!state.accept(old, "website-a", "cloud"));
        check(state.accept(direct, null, "direct"));
        state.setMode(LoginRequestState.Mode.CLOUD);
        long fresh = state.begin("website-a");
        check(!state.accept(old, "website-a", "stale-cloud"));
        check(!state.accept(direct, "website-a", "stale-direct"));
        check(state.accept(fresh, "website-a", "fresh-cloud"));
        state.websiteSessionChanged();
        check(state.value("website-a") == null);
    }

    private static void check(boolean condition) {
        assertions++;
        if (!condition) throw new AssertionError("Stale authorization request escaped its gate");
    }
}
