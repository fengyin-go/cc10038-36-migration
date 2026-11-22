<template>
  <div class="app-shell">
    <aside class="app-side">
      <h1 class="app-title">多源日志采集与解析中心</h1>
      <nav class="nav-list">
        <RouterLink v-for="item in navItems" :key="item.path" :to="item.path" class="nav-item">
          {{ item.label }}
        </RouterLink>
      </nav>
    </aside>
    <main class="app-main">
      <header class="app-head">
        <span class="head-desc">管理多来源日志接入、字段解析、批量去重和水位推进的数据采集平台</span>
        <span class="head-user">当前值班：数据工程组</span>
      </header>
      <RouterView />
    </main>
  </div>
</template>

<script setup lang="ts">
const navItems = [{ label: "运营概览", path: "/" }, { label: "版本化日志交付", path: "/delivery" }, { label: "数据源登记", path: "/source_registry" }, { label: "采集批次", path: "/ingest_batch" }, { label: "字段解析规则", path: "/field_parser" }, { label: "原始日志记录", path: "/raw_records" }, { label: "标准化明细", path: "/parsed_entity" }, { label: "去重引擎", path: "/dedup_engine" }, { label: "水位管理器", path: "/watermark_mgr" }, { label: "重试队列", path: "/retry_queue" }, { label: "格式适配层", path: "/format_adapter" }, { label: "质量校验规则", path: "/quality_check" }, { label: "归档策略", path: "/data_archive" }, { label: "限流控制器", path: "/rate_limiter" }, { label: "解析操作日志", path: "/parse_journal" }, { label: "RPA采集流程", path: "/rpa_workflow" }, { label: "Schema版本", path: "/schema_version" }, { label: "采集告警策略", path: "/alert_policy" }, { label: "数据导出", path: "/data_export" }, { label: "回溯重放引擎", path: "/replay_engine" }, { label: "聚合查询视图", path: "/aggregation_view" }, { label: "采集血缘", path: "/data_lineage" }]
</script>
