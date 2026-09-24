import type { ThemeConfig } from 'antd';
import { theme } from 'antd';

/**
 * SensoryPlex 统一设计语言与色彩体系
 * 定位：工业级、高性能边缘多模态素材基座控制台
 */
export const palette = {
    // 品牌主色调：深邃极客工业蓝，兼具稳定感与前沿智能感
    primary: '#1668dc',
    primaryHover: '#3884f7',
    primaryActive: '#0e4eb5',
    primaryBg: '#e6f0ff',

    // 辅助特征色：青天色，代表流媒体、感知连接与毫秒时间轴
    cyan: '#08979c',
    cyanBg: '#e6fffb',

    // 功能状态语义色
    success: '#10b981', // 翡翠绿：已完成、可调度、就绪、已接纳
    successBg: '#ecfdf5',
    warning: '#f59e0b', // 琥珀黄：待接纳、待准入、降级、警告
    warningBg: '#fffbeb',
    error: '#ef4444', // 珊瑚红：失败、离线、错误、冲突
    errorBg: '#fef2f2',
    info: '#3b82f6', // 天蓝：处理中、注册中、进行中
    infoBg: '#eff6ff',

    // 中性色阶与表面体系
    bgLayout: '#f8fafc', // 主工作区底色 (Slate-50)
    bgContainer: '#ffffff', // 纯白卡片容器
    bgElevated: '#ffffff', // 悬浮层底色
    border: '#e2e8f0', // 细腻分割边框 (Slate-200)
    borderLight: '#f1f5f9', // 极浅边框 (Slate-100)

    // 文字阶梯
    textPrimary: '#0f172a', // 正文标题最黑墨色 (Slate-900)
    textSecondary: '#475569', // 次要描述信息 (Slate-600)
    textMuted: '#94a3b8', // 辅助次级浅灰色 (Slate-400)

    // 侧边栏专属深邃 Slate 科技调
    sidebarBg: '#090d16',
    sidebarCard: '#131b2e',
    sidebarBorder: '#1e293b',
    sidebarText: '#94a3b8',
    sidebarTextActive: '#ffffff',
    sidebarHover: '#1e293b',
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
        fontFamily: `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif`,
        fontFamilyCode: `"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace`,
        borderRadius: 8,
        borderRadiusLG: 10,
        borderRadiusSM: 6,
        boxShadowSecondary:
            '0 4px 20px -2px rgba(15, 23, 42, 0.06), 0 2px 6px -1px rgba(15, 23, 42, 0.04)',
        controlHeight: 36,
        controlHeightLG: 42,
        controlHeightSM: 30,
        fontSize: 14,
        lineHeight: 1.5714,
    },
    components: {
        Button: {
            controlHeight: 36,
            borderRadius: 7,
            fontWeight: 500,
            primaryShadow: '0 2px 4px 0 rgba(22, 104, 220, 0.25)',
        },
        Card: {
            borderRadiusLG: 10,
            colorBorderSecondary: palette.border,
            boxShadowTertiary: '0 1px 3px 0 rgba(15, 23, 42, 0.03)',
        },
        Table: {
            headerBg: '#f8fafc',
            headerColor: palette.textSecondary,
            headerSplitColor: 'transparent',
            rowHoverBg: '#f1f5f9',
            borderColor: '#f1f5f9',
            cellPaddingBlock: 12,
            cellPaddingInline: 16,
            borderRadiusLG: 8,
        },
        Modal: {
            borderRadiusLG: 12,
            headerBg: '#ffffff',
            titleFontSize: 17,
        },
        Tag: {
            borderRadiusSM: 4,
            fontSize: 12,
        },
        Menu: {
            darkItemBg: 'transparent',
            darkItemSelectedBg: palette.primary,
            darkItemHoverBg: '#1e293b',
            itemBorderRadius: 6,
            itemMarginInline: 8,
        },
        Input: {
            borderRadius: 7,
            colorBorder: palette.border,
        },
        Select: {
            borderRadius: 7,
        },
        Tabs: {
            itemSelectedColor: palette.primary,
            inkBarColor: palette.primary,
        },
    },
};
