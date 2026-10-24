package com.huy.authlogin;

public final class AutomationLoginStateTest {
    public static void main(String[] args) {
        AutomationLoginState state=new AutomationLoginState();
        state.ready("task-a","capture-a");
        assert state.canConfirm("capture-a");
        assert !state.canConfirm("capture-b");
        state.invalidate();
        assert !state.canConfirm("capture-a");
        state.ready("task-b","capture-b");
        assert !state.canConfirm("capture-a");
        assert state.submit("capture-b");
        assert !state.canConfirm("capture-b");
        assert "submitted".equals(state.status("capture-b"));
        assert !state.clear("capture-a");
        assert "submitted".equals(state.status("capture-b"));
        assert state.clear("capture-b");
        assert "waiting".equals(state.status("capture-b"));
        System.out.println("AutomationLoginStateTest: stale confirmation and one-use checks passed");
    }
}
