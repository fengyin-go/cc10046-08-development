"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import dedup
from app.routers import resource
from app.routers import chains

# dedup 的固定路径（/api/dedup/board 等）必须先于通用资源路由注册，
# 否则会被 /api/{module}/{entry_id} 先匹配掉。
ROUTERS = [dedup, resource, chains]
