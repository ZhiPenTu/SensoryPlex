import type { ThemeConfig } from 'antd';
import { theme } from 'antd';

/**
 * SensoryPlex 统一设计语言与色彩体系
 * 设计哲学：极简、克制、专业、高质感（Clean Minimalist Tech / Precision Slate）
 * 核心准则：中性色为主（90% 灰阶层次），语义色为辅（10% 精确点缀），坚决杜绝大面积荧光或高饱和色块。
 */
export const palette = {
    // 品牌主色：蓝调精准，沉稳克制、现代科技
    primary: '#2563eb',          // Blue-600
    primaryHover: '#3b82f6',     // Blue-500
    primaryActive: '#1d4ed8',    // Blue-700
    primaryBg: '#eff6ff',        // Blue-50

    // 功能状态语义色
    success: '#059669',           // Emerald-600
    successBg: '#f0fdf4',
    successBorder: '#a7f3d0',

    warning: '#d97706',           // Amber-600
    warningBg: '#fffbeb',
    warningBorder: '#fde68a',

    error: '#dc2626',             // Red-600
    errorBg: '#fef2f2',
    errorBorder: '#fecaca',

    info: '#0284c7',              // Sky-600
    infoBg: '#f0f9ff',
    infoBorder: '#bae6fd',

    // 中性灰阶表面体系
    bgLayout: '#f1f5f9',          // Slate-100（主工作区，比纯白更有层次感）
    bgContainer: '#ffffff',
    bgElevated: '#ffffff',
    border: '#e2e8f0',            // Slate-200
    borderLight: '#f1f5f9',       // Slate-100

    // 字体灰阶阶梯
    textPrimary: '#0f172a',       // Slate-900
    textSecondary: '#475569',     // Slate-600
    textMuted: '#94a3b8',         // Slate-400

    // 侧边栏专属星空深色
    sidebarBg: '#0c111d',
    sidebarCard: '#161d2e',
    sidebarBorder: '#1e2d40',
    sidebarText: '#7d95ae',
    sidebarTextHover: '#c8d9e8',
    sidebarTextActive: '#93c5fd',
    sidebarActiveBg: 'linear-gradient(90deg, rgba(59, 130, 246, 0.14) 0%, rgba(59, 130, 246, 0.06) 100%)',
};

/**
 * Ant Design 全局主题 Token
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
        borderRadius: 6,
        borderRadiusLG: 8,
        borderRadiusSM: 4,
        boxShadow: '0 1px 3px 0 rgba(15, 23, 42, 0.08), 0 1px 2px -1px rgba(15, 23, 42, 0.06)',
        boxShadowSecondary: '0 4px 16px -4px rgba(15, 23, 42, 0.12), 0 2px 8px -4px rgba(15, 23, 42, 0.08)',
        controlHeight: 32,
        controlHeightLG: 38,
        controlHeightSM: 24,
        fontSize: 13,
        fontSizeSM: 12,
        fontSizeLG: 14,
        lineHeight: 1.5,
        padding: 10,
        paddingLG: 16,
        paddingSM: 8,
        paddingXS: 4,
        margin: 10,
        marginLG: 16,
        marginSM: 8,
        marginXS: 4,
        motionDurationMid: '0.15s',
        motionDurationSlow: '0.2s',
    },
    components: {
        Button: {
            controlHeight: 32,
            controlHeightSM: 24,
            borderRadius: 6,
            fontWeight: 500,
            paddingInline: 14,
            paddingInlineSM: 8,
            primaryColor: '#ffffff',
            primaryShadow: '0 1px 3px 0 rgba(37, 99, 235, 0.25)',
            defaultShadow: '0 1px 2px 0 rgba(15, 23, 42, 0.06)',
        },
        Card: {
            borderRadiusLG: 8,
            colorBorderSecondary: palette.border,
            boxShadowTertiary: '0 1px 3px 0 rgba(15, 23, 42, 0.04)',
            headerPadding: 10,
            headerPaddingSM: 8,
            bodyPadding: 14,
            bodyPaddingSM: 10,
            headerFontSize: 13,
            headerFontSizeSM: 12,
        },
        Table: {
            headerBg: '#f8fafc',
            headerColor: '#64748b',
            headerSplitColor: '#f1f5f9',
            rowHoverBg: '#f8fafc',
            borderColor: '#f1f5f9',
            cellPaddingBlock: 8,
            cellPaddingInline: 12,
            cellPaddingBlockSM: 5,
            cellPaddingInlineSM: 8,
            fontSize: 12.5,
            borderRadiusLG: 8,
        },
        Modal: {
            borderRadiusLG: 10,
            headerBg: '#ffffff',
            titleFontSize: 15,
            footerBg: '#fafbfc',
        },
        Tag: {
            borderRadiusSM: 4,
            fontSize: 11.5,
            lineHeight: 1.4,
        },
        Input: {
            controlHeight: 32,
            borderRadius: 6,
            paddingInline: 10,
            colorBorder: palette.border,
            hoverBorderColor: '#93c5fd',
            activeBorderColor: palette.primary,
        },
        Select: {
            controlHeight: 32,
            borderRadius: 6,
        },
        Tabs: {
            itemSelectedColor: palette.primary,
            inkBarColor: palette.primary,
            horizontalItemPadding: '7px 12px',
            horizontalMargin: '0 0 12px 0',
            itemHoverColor: '#3b82f6',
            titleFontSize: 13,
        },
        Statistic: {
            titleFontSize: 12,
            contentFontSize: 20,
        },
        Alert: {
            defaultPadding: '8px 12px',
            withDescriptionPadding: '10px 14px',
            borderRadiusLG: 7,
        },
        Descriptions: {
            itemPaddingBottom: 6,
            itemPaddingEnd: 8,
            titleMarginBottom: 6,
            labelBg: '#f8fafc',
        },
        Form: {
            itemMarginBottom: 14,
            labelFontSize: 13,
        },
        Pagination: {
            itemSize: 28,
            itemSizeSM: 22,
        },
        Badge: {
            fontSize: 11,
        },
        Tooltip: {
            borderRadius: 6,
            fontSize: 12,
        },
        Popconfirm: {
            fontSize: 13,
        },
        Drawer: {
            borderRadiusLG: 0,
            footerPaddingBlock: 12,
            footerPaddingInline: 16,
        },
        Dropdown: {
            borderRadiusLG: 8,
            controlItemBgHover: '#f0f5ff',
        },
    },
};
