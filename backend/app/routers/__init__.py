"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import delivery_router, resource, pipeline_router

# 固定前缀路由必须排在 /api/{module} 之前，否则 /api/delivery/... 会被
# 资源路由当作「模块 + 记录 ID」抢先匹配。
ROUTERS = [pipeline_router, delivery_router, resource]
