"""Orders and returns backend (spec §4.2 domain 2).

Policy: only a `placed` order can be cancelled or have its address changed;
only a `delivered` order can be returned (refused writes raise PolicyError).
"""
from __future__ import annotations

from duplexbench.tools.state import Backend, PolicyError, ToolSchema, obj_schema


class OrdersBackend(Backend):
    domain = "orders"
    SEED = {
        "orders": {
            "1001": {"status": "placed", "email": "ana@example.com", "total": "42.10",
                     "address": "12 Oak St", "items": ["blue mug"]},
            "1002": {"status": "delivered", "email": "ben@example.com", "total": "19.99",
                     "address": "4 Elm Rd", "items": ["desk lamp"]},
        },
        "returns": {},
    }

    def tools(self) -> list[ToolSchema]:
        return [
            ToolSchema("lookup_order", "read", "Look up an order by number.",
                       obj_schema({"order_id": "string"})),
            ToolSchema("cancel_order", "write", "Cancel an order that has not shipped.",
                       obj_schema({"order_id": "string"})),
            ToolSchema("update_shipping_address", "write",
                       "Change the address of an unshipped order.",
                       obj_schema({"order_id": "string", "address": "string"})),
            ToolSchema("create_return", "write", "Open a return for a delivered order.",
                       obj_schema({"order_id": "string", "reason": "string"})),
        ]

    def _t_lookup_order(self, order_id: str) -> dict:
        o = self._row("orders", order_id)
        return {"order_id": order_id, **o, "_anchors": [o["status"]]}

    def _require(self, order_id: str, status: str, action: str) -> dict:
        o = self._row("orders", order_id)
        if o["status"] != status:
            raise PolicyError(f"{action}_not_allowed", f"order {order_id} is {o['status']}")
        return o

    def _t_cancel_order(self, order_id: str) -> dict:
        self._require(order_id, "placed", "cancel")["status"] = "cancelled"
        return {"order_id": order_id, "status": "cancelled", "_anchors": ["cancelled"]}

    def _t_update_shipping_address(self, order_id: str, address: str) -> dict:
        self._require(order_id, "placed", "address_change")["address"] = address
        return {"order_id": order_id, "address": address, "_anchors": [address]}

    def _t_create_return(self, order_id: str, reason: str) -> dict:
        self._require(order_id, "delivered", "return")
        rid = self._next_id("returns", "R")
        self._table("returns")[rid] = {"order_id": order_id, "reason": reason, "status": "open"}
        return {"return_id": rid, "order_id": order_id, "_anchors": [rid]}
