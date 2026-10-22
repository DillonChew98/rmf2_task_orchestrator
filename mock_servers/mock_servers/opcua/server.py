"""Mock OPC UA buffer storage PLC, port 4840.

Hosts all OPC_AMR_* and OPC_PLC_* variables at namespace 4 and
auto-responds to AMR writes with the matching PLC acknowledgements,
mimicking the real buffer storage controller.

    python -m mock_servers.opcua.server [--port 4840] [--delay 1.0]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from asyncua import Server, ua

log = logging.getLogger(__name__)

PORT = 4840
NAMESPACE_URI = "urn:mock:buffer_storage"

# All variables and their types/defaults.
# OPC_AMR_* are written by the workflow (crossflow).
# OPC_PLC_* are set by this mock in response.
VARIABLES: dict[str, tuple[ua.VariantType, object]] = {
    # ── Loading ──
    "OPC_AMR_Send_Request":       (ua.VariantType.Boolean, False),
    "OPC_AMR_Send_Busy":          (ua.VariantType.Boolean, False),
    "OPC_AMR_Send_Complete":      (ua.VariantType.Boolean, False),
    "OPC_PLC_Ready_To_Receive":   (ua.VariantType.Boolean, False),
    "OPC_PLC_Send_Complete_Ack":  (ua.VariantType.Boolean, False),

    # ── Unloading ──
    "OPC_AMR_Unload_TrayID_Request":      (ua.VariantType.String, ""),
    "OPC_AMR_Unload_Request":             (ua.VariantType.Boolean, False),
    "OPC_AMR_Unload_Busy":                (ua.VariantType.Boolean, False),
    "OPC_AMR_Unload_Complete":            (ua.VariantType.Boolean, False),
    "OPC_AMR_Unload_Request_Reject_Ack":  (ua.VariantType.Boolean, False),
    "OPC_PLC_Unload_Request_Ack":         (ua.VariantType.Boolean, False),
    "OPC_PLC_TrayID_Not_Found":           (ua.VariantType.Boolean, False),
    "OPC_PLC_Ready_To_Unload":            (ua.VariantType.Boolean, False),
    "OPC_PLC_Unload_Complete_Ack":        (ua.VariantType.Boolean, False),
}


async def _read(nodes, name):
    return await nodes[name].read_value()


async def _write(nodes, name, value):
    log.info("PLC -> %s = %s", name, value)
    await nodes[name].write_value(value)


async def plc_logic(nodes, delay: float):
    """Poll AMR variables and respond as the PLC would."""
    prev = {name: default for name, (_, default) in VARIABLES.items()}

    while True:
        await asyncio.sleep(0.25)

        cur = {}
        for name in VARIABLES:
            cur[name] = await _read(nodes, name)

        # ── Loading handshake ──

        if cur["OPC_AMR_Send_Request"] and not prev["OPC_AMR_Send_Request"]:
            log.info("AMR requested loading — acknowledging in %.1fs", delay)
            await asyncio.sleep(delay)
            await _write(nodes, "OPC_PLC_Ready_To_Receive", True)

        if cur["OPC_AMR_Send_Complete"] and not prev["OPC_AMR_Send_Complete"]:
            log.info("AMR completed loading — acknowledging in %.1fs", delay)
            await asyncio.sleep(delay)
            await _write(nodes, "OPC_PLC_Send_Complete_Ack", True)

        if not cur["OPC_AMR_Send_Request"] and prev["OPC_AMR_Send_Request"]:
            log.info("AMR cleared loading signals — resetting PLC")
            await _write(nodes, "OPC_PLC_Ready_To_Receive", False)
            await _write(nodes, "OPC_PLC_Send_Complete_Ack", False)

        # ── Unloading handshake ──

        if cur["OPC_AMR_Unload_Request"] and not prev["OPC_AMR_Unload_Request"]:
            tray_id = cur["OPC_AMR_Unload_TrayID_Request"]
            log.info("AMR requested unloading (tray=%s) — acknowledging in %.1fs", tray_id, delay)
            await asyncio.sleep(delay)
            await _write(nodes, "OPC_PLC_Unload_Request_Ack", True)
            await asyncio.sleep(delay)
            await _write(nodes, "OPC_PLC_Ready_To_Unload", True)

        if cur["OPC_AMR_Unload_Complete"] and not prev["OPC_AMR_Unload_Complete"]:
            log.info("AMR completed unloading — acknowledging in %.1fs", delay)
            await asyncio.sleep(delay)
            await _write(nodes, "OPC_PLC_Unload_Complete_Ack", True)

        if not cur["OPC_AMR_Unload_Request"] and prev["OPC_AMR_Unload_Request"]:
            log.info("AMR cleared unloading signals — resetting PLC")
            await _write(nodes, "OPC_PLC_Unload_Request_Ack", False)
            await _write(nodes, "OPC_PLC_TrayID_Not_Found", False)
            await _write(nodes, "OPC_PLC_Ready_To_Unload", False)
            await _write(nodes, "OPC_PLC_Unload_Complete_Ack", False)

        prev = cur


async def run(host: str = "0.0.0.0", port: int = PORT, delay: float = 1.0):
    server = Server()
    await server.init()
    server.set_endpoint(f"opc.tcp://{host}:{port}")
    server.set_server_name("Mock Buffer Storage PLC")
    server.set_security_policy([ua.SecurityPolicyType.NoSecurity])

    # Register placeholder namespaces so our variables land at index 4.
    await server.register_namespace("urn:mock:padding_ns2")
    await server.register_namespace("urn:mock:padding_ns3")
    ns_idx = await server.register_namespace(NAMESPACE_URI)
    assert ns_idx == 4, f"Expected namespace index 4, got {ns_idx}"

    objects = server.nodes.objects
    folder = await objects.add_folder(ns_idx, "BufferStorage")

    nodes = {}
    for name, (variant_type, default) in VARIABLES.items():
        var = await folder.add_variable(
            ua.NodeId(name, ns_idx),
            name,
            ua.Variant(default, variant_type),
        )
        await var.set_writable()
        nodes[name] = var

    log.info("OPC UA mock on opc.tcp://%s:%d  ns=%d  delay=%.1fs", host, port, ns_idx, delay)

    async with server:
        await plc_logic(nodes, delay)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("asyncua").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(description="Mock OPC UA buffer storage PLC")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--delay", type=float, default=1.0,
                        help="PLC response delay in seconds (default: 1.0)")
    args = parser.parse_args()

    asyncio.run(run(args.host, args.port, args.delay))


if __name__ == "__main__":
    main()
