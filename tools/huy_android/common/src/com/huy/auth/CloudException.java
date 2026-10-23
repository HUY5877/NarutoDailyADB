package com.huy.auth;

/** Messages are fixed locally; response bodies and network errors never reach the UI. */
public final class CloudException extends Exception {
    private final boolean authenticationRequired;

    CloudException(String message) { this(message, false); }

    CloudException(String message, boolean authenticationRequired) {
        super(message);
        this.authenticationRequired = authenticationRequired;
    }

    public boolean isAuthenticationRequired() { return authenticationRequired; }
}
