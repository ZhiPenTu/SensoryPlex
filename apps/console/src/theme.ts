import type { ThemeConfig } from 'antd';
import { theme } from 'antd';

/**
 * SensoryPlex 统一设计语言与色彩体系
 * 设计哲学：极简、克制、专业、高质感（Clean Minimalist Tech / Precision Slate）
 * 核心准则：中性色为主（90% 灰阶层次），语义色为辅（10% 精确点缀），坚决杜绝大面积荧光或高饱和色块。
 */
export const palette = {
    // 品牌主色：克莱因深蓝，沉稳克制、现代科技，不刺眼
    primary: '#1d4ed8', // Blue-700
    primaryHover: '#2563eb', // Blue-600
    primaryActive: '#1e40af', // Blue-800
    primaryBg: '#eff6ff', // Blue-50 (仅用于悬浮微底或轻标签)

    // 功能状态语义色（仅用于图标、Tag、状态点，严禁大面积铺底色）
    success: '#059669', // Emerald-600：健康就绪、完成
    successBg: '#f0fdf4',
    successBorder: '#bbf7d0',

    warning: '#d97706', // Amber-600：候选审批、待准入
    warningBg: '#fffbeb',
    warningBorder: '#fde68a',

    error: '#dc2626', // Red-600：离线、错误、失败
    errorBg: '#fef2f2',
    errorBorder: '#fecaca',

    info: '#0284c7', // Sky-600：处理中、信息提示
    infoBg: '#f0f9ff',
    infoBorder: '#bae6fd',

    // 中性灰阶表面体系（全站基底）
    bgLayout: '#f8fafc', // 主工作区浅冷灰底 (Slate-50)
    bgContainer: '#ffffff', // 卡片纯白容器，保持视线清爽
    bgElevated: '#ffffff', // 弹窗与悬浮层
    border: '#e2e8f0', // 统一标准极细分割线 (Slate-200)
    borderLight: '#f1f5f9', // 极淡分割线 (Slate-100)

    // 字体灰阶阶梯
    textPrimary: '#0f172a', // 主标题正文深炭黑 (Slate-900)
    textSecondary: '#475569', // 次要说明与副标题 (Slate-600)
    textMuted: '#94a3b8', // 占位符与辅助信息 (Slate-400)

    // 侧边栏专属石板深灰科技调（极致优雅深色，告别纯黑死板）
    sidebarBg: '#0b0f19', // 深石板黑 (Slate-950)
    sidebarCard: '#131b2a', // 侧栏卡片底色
    sidebarBorder: '#1e293b', // 侧栏极细边框 (Slate-800)
    sidebarText: '#94a3b8', // 默认菜单字色
    sidebarTextHover: '#f8fafc',
    sidebarTextActive: '#ffffff',
    sidebarActiveBg: 'rgba(255, 255, 255, 0.07)', // 克制半透明微光，告别刺眼大色块
};

/**
 * Ant Design 全局主题 Token 统筹
 */
export const antdTheme: ThemeConfig = {
    algorithm: theme.defaultAlgorithm,
    token: {
        colorPrimary: palette.primary,
        colorSuccess: palette.success,
        colorWarning: palette.warning,
        colorError: palette.error,
        colorInfo: palette.info,
        colorBgLayout: palette.bgLayout,
        colorBgContainer: palette.bgContainer,
        colorBorder: palette.border,
        colorBorderSecondary: palette.borderLight,
        colorText: palette.textPrimary,
        colorTextSecondary: palette.textSecondary,
        colorTextTertiary: palette.textMuted,
        fontFamily: `-apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif`,
        fontFamilyCode: `"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace`,
        borderRadius: 8,
        borderRadiusLG: 10,
        borderRadiusSM: 6,
        boxShadowSecondary:
            '0 1px 3px 0 rgba(15, 23, 42, 0.05), 0 1px 2px -1px rgba(15, 23, 42, 0.05)',
        controlHeight: 36,
        controlHeightLG: 42,
        controlHeightSM: 30,
        fontSize: 14,
        lineHeight: 1.5714,
    },
    components: {
        Button: {
            controlHeight: 36,
            borderRadius: 6,
            fontWeight: 500,
            primaryColor: '#ffffff',
            primaryShadow: '0 1px 2px 0 rgba(29, 78, 216, 0.2)',
        },
        Card: {
            borderRadiusLG: 10,
            colorBorderSecondary: palette.border,
            boxShadowTertiary: '0 1px 2px 0 rgba(15, 23, 42, 0.03)',
        },
        Table: {
            headerBg: '#f8fafc',
            headerColor: palette.textSecondary,
            headerSplitColor: 'transparent',
            rowHoverBg: '#f8fafc',
            borderColor: '#f1f5f9',
            cellPaddingBlock: 12,
            cellPaddingInline: 16,
            borderRadiusLG: 8,
        },
        Modal: {
            borderRadiusLG: 12,
            headerBg: '#ffffff',
            titleFontSize: 16,
        },
        Tag: {
            borderRadiusSM: 4,
            fontSize: 12,
            lineHeight: 1.6,
        },
        Input: {
            borderRadius: 6,
            colorBorder: palette.border,
        },
        Select: {
            borderRadius: 6,
        },
        Tabs: {
            itemSelectedColor: palette.primary,
            inkBarColor: palette.primary,
        },
        Statistic: {
            titleFontSize: 12,
            contentFontSize: 24,
        },
    },
};
