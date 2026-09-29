import * as React from "react"
import * as TabsPrimitive from "@radix-ui/react-tabs"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

const Tabs = TabsPrimitive.Root

const tabsListVariants = cva(
  "text-muted-foreground",
  {
    variants: {
      variant: {
        segmented:
          "inline-flex h-9 items-center justify-center rounded-lg bg-muted p-1",
        underline:
          "flex h-10 min-w-0 max-w-full shrink-0 items-end gap-5 overflow-x-auto overflow-y-hidden border-b border-edge-subtle",
        vertical:
          "flex w-full flex-col items-stretch gap-1",
      },
    },
    defaultVariants: { variant: "segmented" },
  },
)

type TabsVariant = NonNullable<VariantProps<typeof tabsListVariants>["variant"]>
const TabsVariantContext = React.createContext<TabsVariant>("segmented")

interface TabsListProps
  extends React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>,
    VariantProps<typeof tabsListVariants> {}

const TabsList = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.List>,
  TabsListProps
>(({ className, variant = "segmented", ...props }, ref) => (
  <TabsVariantContext.Provider value={variant ?? "segmented"}>
    <TabsPrimitive.List
      ref={ref}
      className={cn(tabsListVariants({ variant }), className)}
      {...props}
    />
  </TabsVariantContext.Provider>
))
TabsList.displayName = TabsPrimitive.List.displayName

const triggerVariants = cva(
  "text-tab inline-flex min-h-8 items-center justify-center whitespace-nowrap transition-[color,background-color,border-color] duration-feedback focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:pointer-events-none disabled:opacity-50",
  {
    variants: {
      variant: {
        segmented:
          "rounded-md px-3 py-1 data-[state=active]:bg-background data-[state=active]:text-foreground",
        underline:
          "relative -mb-px h-10 border-b-2 border-transparent px-0.5 data-[state=active]:border-focus data-[state=active]:text-foreground",
        vertical:
          "w-full justify-start rounded-md border-l-2 border-transparent px-3 py-2 text-left data-[state=active]:border-focus data-[state=active]:bg-primary/[0.07] data-[state=active]:text-foreground",
      },
    },
  },
)

const TabsTrigger = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(({ className, ...props }, ref) => {
  const variant = React.useContext(TabsVariantContext)
  return (
    <TabsPrimitive.Trigger
      ref={ref}
      className={cn(triggerVariants({ variant }), className)}
      {...props}
    />
  )
})
TabsTrigger.displayName = TabsPrimitive.Trigger.displayName

const TabsContent = React.forwardRef<
  React.ElementRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Content
    ref={ref}
    className={cn(
      "ui-tab-content mt-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
      className,
    )}
    {...props}
  />
))
TabsContent.displayName = TabsPrimitive.Content.displayName

export { Tabs, TabsList, TabsTrigger, TabsContent }
