# Single-Project ingress boundary

The reference API/Worker process is bound to exactly one GitLab Project. The webhook secret and Project ID are required at startup. Every accepted event must pass both the secret check and an exact Project ID match before it can enqueue or wake work.

Supporting multiple Projects in one process would require a registry mapping Project IDs to independent repositories, policies, secrets, and credentials. That is outside v0.1 and must not be approximated by accepting an arbitrary payload Project ID while using one fixed Worker configuration.
