package com.huy.authlogin;

/** One-use confirmation identity; the credential itself stays in AgentActivity. */
public final class AutomationLoginState {
    private String taskId,captureId,state="waiting";
    public synchronized void ready(String task,String capture) { taskId=task; captureId=capture; state="ready"; }
    public synchronized boolean canConfirm(String capture) { return capture!=null && capture.equals(captureId) && "ready".equals(state); }
    public synchronized boolean submit(String capture) { if(!canConfirm(capture))return false;state="submitted";return true; }
    public synchronized String status(String capture) { return capture!=null && capture.equals(captureId) ? state : "waiting"; }
    public synchronized void invalidate() { taskId=null;captureId=null;state="waiting"; }
    public synchronized boolean clear(String capture) { if(capture==null||!capture.equals(captureId))return false;invalidate();return true; }
    public synchronized String captureId() { return captureId; }
}
