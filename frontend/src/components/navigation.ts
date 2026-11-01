import {
  BarChart3,
  Gauge,
  KeyRound,
  LibraryBig,
  PlugZap,
  Route,
  Waypoints,
  type LucideIcon,
} from 'lucide-react'

export interface NavItem {
  to: string
  label: string
  shortLabel: string
  description: string
  icon: LucideIcon
}

export interface NavGroup {
  label: string
  items: NavItem[]
}

export const NAV_GROUPS: NavGroup[] = [
  {
    label: 'Workspace',
    items: [
      {
        to: '/',
        label: 'Overview',
        shortLabel: 'Overview',
        description: 'Live gateway pulse',
        icon: Gauge,
      },
      {
        to: '/catalog',
        label: 'Catalog',
        shortLabel: 'Catalog',
        description: 'Capabilities & pricing',
        icon: LibraryBig,
      },
      {
        to: '/models',
        label: 'Model Health',
        shortLabel: 'Health',
        description: 'Provider operations',
        icon: BarChart3,
      },
    ],
  },
  {
    label: 'Operate',
    items: [
      {
        to: '/explorer',
        label: 'Routing Explorer',
        shortLabel: 'Explorer',
        description: 'Decisions & fallbacks',
        icon: Route,
      },
      {
        to: '/analytics',
        label: 'Analytics',
        shortLabel: 'Analytics',
        description: 'Traffic & cost trends',
        icon: Waypoints,
      },
      {
        to: '/keys',
        label: 'API Keys',
        shortLabel: 'Keys',
        description: 'Demo access & usage',
        icon: KeyRound,
      },
      {
        to: '/providers',
        label: 'Provider Keys',
        shortLabel: 'Providers',
        description: 'Encrypted BYOK credentials',
        icon: PlugZap,
      },
    ],
  },
]

export const NAV_ITEMS: NavItem[] = NAV_GROUPS.flatMap((group) => group.items)

export function findNavItem(pathname: string): NavItem | undefined {
  if (pathname === '/') return NAV_ITEMS[0]
  return NAV_ITEMS.find((item) => item.to !== '/' && pathname.startsWith(item.to))
}
