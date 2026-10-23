package com.example.auth;

import java.util.Optional;

/**
 * Handles user authentication and session management.
 * Intentionally simple — exists to demonstrate high/critical tagging.
 */
public class AuthService {

    private final TokenStore tokenStore;
    private final UserRepository userRepository;
    private final AuditLogger auditLogger;

    public AuthService(TokenStore tokenStore, UserRepository userRepository, AuditLogger auditLogger) {
        this.tokenStore = tokenStore;
        this.userRepository = userRepository;
        this.auditLogger = auditLogger;
    }

    /** Validates a JWT and returns the associated user ID, or empty if invalid. */
    public Optional<String> validateToken(String token) {
        if (token == null || token.isBlank()) {
            return Optional.empty();
        }
        try {
            String userId = tokenStore.verify(token);
            auditLogger.log("TOKEN_VALIDATED", userId);
            return Optional.of(userId);
        } catch (TokenExpiredException e) {
            auditLogger.log("TOKEN_EXPIRED", token);
            return Optional.empty();
        } catch (InvalidSignatureException e) {
            auditLogger.log("TOKEN_INVALID_SIGNATURE", token);
            return Optional.empty();
        }
    }

    /** Authenticates a user by username/password and returns a new session token. */
    public String login(String username, String rawPassword) {
        User user = userRepository.findByUsername(username)
                .orElseThrow(() -> new AuthException("Unknown user: " + username));

        if (!user.checkPassword(rawPassword)) {
            auditLogger.log("LOGIN_FAILED", username);
            throw new AuthException("Invalid credentials");
        }

        String sessionToken = tokenStore.issue(user.getId());
        auditLogger.log("LOGIN_SUCCESS", username);
        return sessionToken;
    }

    /** Revokes all active sessions for a user. */
    public void revokeAllSessions(String userId) {
        int count = tokenStore.revokeAll(userId);
        auditLogger.log("SESSIONS_REVOKED", userId + " count=" + count);
    }

    /** Returns the display name for a user ID. No auth logic. */
    public String getDisplayName(String userId) {
        return userRepository.findById(userId)
                .map(User::getDisplayName)
                .orElse("Unknown");
    }
}
