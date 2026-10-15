# Coursmith · 智课工坊

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

根据项目说明与现有代码：学习者在手机和电脑上浏览、兑换和阅读课程；单 owner 管理员在浏览器内制作课程草稿、审核、管理商品、订单与交付。

## Product Purpose

把课程从私有制作草稿交付为可在线阅读的学习内容。用户已确认本次重设计同时覆盖学习网站和运营后台。

## Capabilities and Constraints

- 现有技术栈为 FastAPI、Jinja2、原生 CSS 和 JavaScript。
- 第三方店铺收款，本站提供激活码兑换、学习凭证、设备恢复、进度与按权益提供的下载。
- 后台包含课程制作、人工审核提示、商品分类、发码批次、订单、历史权益核验与审计。
- 草稿生成与编辑不等于人工审核完成；期限、下载格式和版本承诺必须保留原有真实规则。
- 表单继续由服务器处理，保留 CSRF、修订检查、字段恢复和无 JavaScript 操作。
- 本次改动目标为新版部署目录 m2a-continuation/course_platform；线上服务器地址与部署授权尚未提供。

## Brand Commitments

保留智课工坊 / Coursmith 品牌名与中文产品界面。不新增虚构学员数、评价、价格或课程质量保证。

## Evidence on Hand

项目 README、部署手册、现有公共和后台路由、课程 manifest、现有测试。本次视觉世界由重设计请求授权，具体视觉选择由实现者负责。
