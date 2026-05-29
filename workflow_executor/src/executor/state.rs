// Handle for the executor.
// Diagram execution is handled by crossflow's built-in executor.
#[derive(Clone)]
pub struct ExecutorHandle {
    // Base URL of the executor HTTP server (e.g. "http://127.0.0.1:2727")
    pub executor_url: String,
}
