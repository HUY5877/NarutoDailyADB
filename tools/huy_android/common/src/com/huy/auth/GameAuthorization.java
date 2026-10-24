package com.huy.auth;

/** An in-memory authorization that can be explicitly returned to the requesting game. */
public interface GameAuthorization {
    String responseJson(long now);

    /** The last four OpenID characters, not a QQ account number. */
    String accountHint();
}
