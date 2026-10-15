# RMF2 Task Orchestrator

**RMF2 Task Orchestrator** is a workflow executor for multi-robot task coordination, built on top of [Bevy ECS](https://bevy.org/learn/quick-start/getting-started/ecs/) and [OpenRMF's Crossflow](https://github.com/open-rmf/crossflow).

## Getting Started

**Prerequisites:** 
- If running locally: 
  - Install & run MQTT broker (eg.[Mosquitto](https://mosquitto.org/download/))

  - Install & run AMQP server (eg. [RabbitMQ](https://www.rabbitmq.com/docs/download))

- Docker Desktop: [Windows](https://docs.docker.com/desktop/setup/install/windows-install/), [macOS](https://docs.docker.com/desktop/setup/install/mac-install/), [Linux](https://docs.docker.com/desktop/setup/install/linux/)

### Running with Docker Compose (production)

Start just the Task Orchestrator (assumes Postgres, RabbitMQ, and MQTT are
already running on the network):

```bash
docker compose up --build
```

Set the following environment variables for your deployment (via `.env` file
or in `docker-compose.yml` under `environment`):

| Variable | Description | Example |
|----------|-------------|---------|
| `AMQP_HOST` | RabbitMQ host | `rabbitmq.lab.local` |
| `AMQP_PORT` | RabbitMQ port | `5672` |
| `AMQP_USERNAME` | RabbitMQ user | `rmf2` |
| `AMQP_PASSWORD` | RabbitMQ password | `secretpassword` |
| `MQTT_HOST` | MQTT broker host | `mosquitto.lab.local` |
| `MQTT_PORT` | MQTT broker port | `1883` |
| `MIR_AUTH` | MiR API auth header | `Basic <base64>` |
| `RMF2_TO__DATABASE__URL` | Postgres connection string | `postgres://user:pass@db-host:5432/rmf2_task_orchestrator` |
| `RMF2_TO__GRPC__CA_CERT` | Path to CA cert for instrument TLS | `assets/certs/ca.crt` |
| `LOG_LEVEL` | Rust log filter | `rmf2_task_orchestrator=info` |

Also update the instrument endpoints in `lab.toml` to point to the real
devices on your lab network.

### Running with Docker Compose (local dev)

Start Postgres, RabbitMQ, Mosquitto, and the Task Orchestrator together:

```bash
docker compose --profile local up --build
```

This reads environment variables from `.env.development` (e.g. `MIR_AUTH`).
Copy the example file to get started:

```bash
cp .env.development.example .env.development
```
The Task Orchestrator runs with `network_mode: host`, so it shares the host
network — mock servers and other services on `127.0.0.1` are reachable directly.

To run the mock instrument and robot servers (SiLA on 50061–50064, MiR on 50065, UR on 50066):

```bash
cd mock_servers
./run_all.sh
```

> **Note:** The mock servers require a Python venv. Set it up with:
> ```bash
> cd mock_servers
> uv venv --python 3.12 .venv && uv pip install -e '.[dev]'
> ```

### Running natively

**Compile & run the Task Orchestrator:**

```bash
cargo run
```

You will need MQTT (e.g. Mosquitto), AMQP (e.g. RabbitMQ), and Postgres
running separately.

### Run an example workflow

```bash
curl -X POST http://localhost:2727/api/executor/run \
  -H 'Content-Type: application/json' \
  -d '{"diagram": '"$(cat diagrams/mqtt_examples/mqtt_listen_consume.json)"', "request": {}}'
```

Alternatively, the workflow can be edited and run on the live editor at <http://localhost:2727>.

![Diagram Editor](docs/images/diagram_editor_example.png)


## Configuration

### `config.toml` — service connections

| Section | Key | Description |
|---------|-----|-------------|
| `[http]` | `host`, `port` | Address the TO listens on. Use `0.0.0.0` to expose outside the host. |
| `[amqp]` | `host`, `port` | RabbitMQ broker. |
| `[mqtt]` | `host`, `port` | MQTT broker (e.g. Mosquitto). |
| `[database]` | `url` | Postgres connection string. |
| `[grpc]` | `ca_cert`, `domain_name`, `client_cert`, `client_key` | TLS settings for gRPC calls to instruments. Uncomment and set for production. |
| `[app]` | `template_directory` | Path to section/workflow templates. |

All values can be overridden with environment variables using the
`RMF2_TO__` prefix (e.g. `RMF2_TO__AMQP__HOST=rabbitmq`).

### `lab.toml` — instrument and robot endpoints

| Key | Description |
|-----|-------------|
| `mir_host` | MiR fleet robot REST API |
| `ur_host` | UR arm mock/real server |
| `dsc_host`, `tga_host`, `ftir_host` | SiLA2 gRPC instrument servers |
| `*_waypoint` | Named positions for the MiR robot |

In production, replace the `127.0.0.1` addresses in `lab.toml` with the actual
IPs or hostnames of the instruments on the lab network.

### Environment variables

| Variable | Description |
|----------|-------------|
| `MIR_AUTH` | MiR API authorization header (Basic auth). Required for workflows that drive the robot. |
| `RMF2_TO__GRPC__CA_CERT` | Path to CA cert for TLS gRPC connections to instruments. |
| `RUST_LOG` | Log level filter (e.g. `rmf2_task_orchestrator=info`). |

## Documentation

- **Platform**: Ubuntu 22.04+
- **Protocol Support**: MQTT, AMQP

Detailed Documentation

- [Task Interfaces](./docs/interfaces.md)

- [Node Types](./docs/nodes.md)

## License

[Apache 2.0](http://www.apache.org/licenses/LICENSE-2.0.html)
