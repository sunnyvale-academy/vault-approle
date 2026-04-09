## Architecture

Below is a sequence diagram illustrating the three main phases of the demo: Vault Configuration, Credential Delivery by Ansible, and the Application Runtime flow.

```mermaid
sequenceDiagram
    participant A as Ansible (Trusted Entity)
    participant V as HashiCorp Vault
    participant W as Web App (Python/Flask)
    participant D as PostgreSQL Database

    Note over A,V: Phase 1: Configuration (Root Token)
    A->>V: Enable AppRole & DB Engines
    A->>V: Configure DB Connection & Role
    A->>V: Create CICD Policy & Generate Dynamic Token

    Note over A,W: Phase 2: Secure Token Delivery (CICD Token)
    A->>V: Fetch RoleID & Generate SecretID (Using CICD Token)
    A->>V: Login via AppRole (X-Vault-Wrap-TTL)
    V-->>A: Wrapping Token (Single-Use, 60s)
    A->>W: Start App (Pipes Wrapping Token via stdin)
    Note over A: Ansible Terminated

    Note over W,V: Phase 3: Application Runtime
    W->>V: Unwrap Token (sys/wrapping/unwrap)
    V-->>W: Long-lived VAULT_TOKEN
    
    rect rgb(200, 220, 240)
        Note right of W: Background Thread
        W->>V: Periodic Token Renewal (Every 30m)
    end

    loop Every Request
        alt Credentials in Cache & Valid?
            Note right of W: Use Cached DB Credentials
        else Cache Empty or Expired?
            W->>V: Request Dynamic DB Credentials
            V-->>W: New DB User & Password
            Note right of W: Update Cache
        end
        W->>D: Connect via Dynamic Credentials
        D-->>W: Query Results
    end
```

> [!TIP]
> You can also find a detailed Architecture Diagram in the [architecture.drawio](architecture.drawio) file.

## Credential Mechanics: RoleID vs. SecretID

The AppRole backend is designed for machine-to-machine authentication, implementing a multi-factor approach by requiring two distinct identifiers to grant access. This follows the **Principle of Least Privilege** and ensures that a compromise of one component does not necessarily lead to a full system compromise.

| Component | Nature | Analogy | Storage / Visibility | Behavior in this Demo |
| :--- | :--- | :--- | :--- | :--- |
| **RoleID** | **Static** | Username | Application Config / Public | Assigned during role creation. It is considered a semi-public identifier. |
| **SecretID** | **Dynamic** | Password | **External / Hidden** | Generated on-demand by the "Trusted Entity". It is **single-use** with a **5m TTL**. |

### The "Zero-Knowledge" Principle
A key security feature of this implementation is that the application itself has **zero-knowledge** of its own `SecretID`. 
- **RoleID** is static and can be hardcoded in Ansible playbook.
- **SecretID** is high-privilege and dynamic. By keeping it entirely outside the application's environment (it only exists in memory within the Ansible process), we significantly reduce the blast radius if the application's environment is compromised.

### How are credentials managed and refreshed?
A critical security principle in this demo is that **the application never sees the SecretID** and cannot refresh its own credentials if they are completely lost.

Instead:
1.  **Single-Use SecretID**: In [playbook-setup-vault.yml](/ansible/playbook-setup-vault.yml), the AppRole is configured with `secret_id_num_uses: 1` and a short `5m` TTL.
2.  **Trusted Entity (Ansible)**: Ansible acts as the Trusted Entity. It:
    -   Authenticates with Vault using its own credentials.
    -   Generates a **single-use SecretID**.
    -   Performs a login to obtain a **wrapped Vault Token** (response-wrapping).
    -   Delivers this wrapped token to the application via standard input (`stdin`).
3.  **Application Responsibility**:
    -   The application reads the wrapped token from `stdin`.
    -   It **unwraps** the token to obtain the actual `VAULT_TOKEN`.
    -   Because the token is periodic, the application starts a **background thread** to renew its own token indefinitely (as long as it continues to run).

This creates a "separation of concerns" where the app only uses a short-lived delivery mechanism (the wrapped token) and then manages its own session lifecycle, while the high-privilege `SecretID` is never exposed to the application environment.

### Summary of Tokens and Credentials

| Credential / Token           | Issuer / Creator                       | Consumer           | TTL / Lifetime                        | Purpose                                                                                   |
| :--------------------------- | :------------------------------------- | :----------------- | :------------------------------------ | :---------------------------------------------------------------------------------------- |
| **Root Token**               | Vault (Startup)                        | Ansible (Setup)    | Infinite                              | Initial setup of Vault AppRole auth, policies, and DB secret engines.                     |
| **CICD Token**               | Ansible (Setup) via Root               | Ansible (Start)    | System Default (32 days)              | Securely fetch `RoleID` and generate a `SecretID` under a restricted policy for app init. |
| **RoleID**                   | Vault AppRole                          | Ansible (Start)    | Infinite                              | Static identifier acting as the username for the application.                             |
| **SecretID**                 | Vault AppRole (via Ansible CICD Token) | Ansible (Start)    | `5m` (Single-use)                     | High-privilege dynamic password acting as the second factor for AppRole login.            |
| **Wrapping Token**           | Vault (AppRole Login)                  | Application        | `60s` (Single-use)                    | Securely deliver the actual payload Vault Token to the application through `stdin`.       |
| **App VAULT_TOKEN**          | Vault (Unwrapped by Application)       | Application        | `1h` (Periodic, Renewable indefinitely)| Authorize the application to fetch dynamic database credentials.                          |
| **Database Credentials**     | Vault DB Engine                        | Application        | Default `1h` (Max `24h`)              | Authenticate PostgreSQL connections dynamically.                                          |

> [!NOTE]
> **What happens if the `CICD Token` expires?** If the token used by `playbook-start-app.yml` expires, it will fail to start the application. In a real-world production environment, the static CICD token is completely replaced by **Identity Federation via OIDC / JWT**. The CI/CD pipeline (e.g., GitHub Actions, GitLab CI/CD) natively passes its temporary runner identity to Vault on every execution to dynamically fetch a *fresh* short-lived token per job. For this lab, if the token expires (typically after 32 days), you simulate a fresh pipeline run by re-running the setup playbook (`ansible-playbook -i ansible/inventory ansible/playbook-setup-vault.yml`) to issue a new valid CICD token.
## Overview

## Prerequisites

- [Docker](https://www.docker.com/) and [Docker Compose](https://docs.docker.com/compose/)
- [Ansible](https://www.ansible.com/)
- [Python 3.9+](https://www.python.org/)
- [pip](https://pip.pypa.io/)

## Setup and Usage

### 1. Start Infrastructure
Start Vault and PostgreSQL:
```bash
docker-compose up -d
```

### 2. Configure Vault (via Ansible)
Run the setup playbook to configure the AppRole and the Database Secret Engine. You can run this from the root:
```bash
ansible-playbook -i ansible/inventory ansible/playbook-setup-vault.yml
```

### 3. Fetch Credentials and Run App (via Ansible)
Run the credentials playbook. It will fetch the `RoleID`, generate a `SecretID`, perform the login, and automatically start the Python application by piping the `VAULT_TOKEN` into its `stdin`:
```bash
ansible-playbook -i ansible/inventory ansible/playbook-start-app.yml
```

The app will start asynchronously and listen on `http://localhost:5001`. The Ansible playbook will terminate immediately after starting the app. The token never enters the environment variables or process list.

### 4. Verify the Demo
Access the application's data endpoint in another terminal:
```bash
curl http://localhost:5001/data
```
The response will show:
- `db_user`: The dynamic username from Vault.
- `cached_creds_used`: `true` if the app used its local cache.
- `expiry`: The timestamp when the current credentials will expire.

## How it handles Expiration
The application uses a `VaultManager` class and Vault's **Periodic Tokens** to ensure continuous operation:
1.  **Wrapped Token Delivery**: Ansible performs the initial AppRole login and passes a wrapped token to the app via `stdin`. The app unwraps this to get its actual `VAULT_TOKEN`.
2.  **Indefinite Renewal**: The AppRole is configured with a `token_period`. This means as long as the token is renewed within its period, it never expires and can be renewed indefinitely.
3.  **Background Renewal**: The application starts a background thread that automatically calls `renew_self()` every 30 minutes.
4.  **No SecretID in App**: The application never sees the `SecretID`. It only receives a short-lived (60s) wrapping token that can only be unwrapped once.

## Security Note
This demo originally used a `root` token for all Ansible interactions. It has since been updated to model least privilege: the initial setup playbook uses the `root` token to configure Vault and generate a restricted dynamic `cicd` token. The start-app playbook then drops its privileges and perfectly acts as the CI/CD pipeline role—using only the `cicd` token to fetch credentials and bootstrap the application. Dynamic DB credentials ensure that the application never has long-term credentials stored in its configuration.
