locals {
  private_dns_zones = {
    blob   = "privatelink.blob.core.windows.net"
    queue  = "privatelink.queue.core.windows.net"
    table  = "privatelink.table.core.windows.net"
    cosmos = "privatelink.documents.azure.com"
  }

  private_endpoint_specs = {
    payload_blob = {
      resource_id = azapi_resource.payload.id
      subresource = "blob"
      dns_key     = "blob"
    }
    control_blob = {
      resource_id = azapi_resource.control_host.id
      subresource = "blob"
      dns_key     = "blob"
    }
    control_queue = {
      resource_id = azapi_resource.control_host.id
      subresource = "queue"
      dns_key     = "queue"
    }
    control_table = {
      resource_id = azapi_resource.control_host.id
      subresource = "table"
      dns_key     = "table"
    }
    inference_blob = {
      resource_id = azapi_resource.inference_host.id
      subresource = "blob"
      dns_key     = "blob"
    }
    inference_queue = {
      resource_id = azapi_resource.inference_host.id
      subresource = "queue"
      dns_key     = "queue"
    }
    inference_table = {
      resource_id = azapi_resource.inference_host.id
      subresource = "table"
      dns_key     = "table"
    }
    cosmos_sql = {
      resource_id = azurerm_cosmosdb_account.main.id
      subresource = "Sql"
      dns_key     = "cosmos"
    }
  }
}

resource "azurerm_virtual_network" "main" {
  count               = var.private_networking ? 1 : 0
  name                = format("vnet-%s-ptu-batch", var.environment_name)
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  address_space       = [var.vnet_address_space]
}

resource "azurerm_subnet" "apps" {
  count                = var.private_networking ? 1 : 0
  name                 = "snet-apps"
  resource_group_name  = azurerm_resource_group.main.name
  virtual_network_name = azurerm_virtual_network.main[0].name
  address_prefixes     = [cidrsubnet(var.vnet_address_space, 7, 0)]

  delegation {
    name = "aca-infrastructure"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
    }
  }
}

resource "azurerm_subnet" "private_endpoints" {
  count                             = var.private_networking ? 1 : 0
  name                              = "snet-private-endpoints"
  resource_group_name               = azurerm_resource_group.main.name
  virtual_network_name              = azurerm_virtual_network.main[0].name
  address_prefixes                  = [cidrsubnet(var.vnet_address_space, 8, 2)]
  private_endpoint_network_policies = "Disabled"
}

resource "azurerm_private_dns_zone" "private" {
  for_each            = var.private_networking ? local.private_dns_zones : {}
  name                = each.value
  resource_group_name = azurerm_resource_group.main.name
}

resource "azurerm_private_dns_zone_virtual_network_link" "private" {
  for_each              = azurerm_private_dns_zone.private
  name                  = format("%s-%s", var.environment_name, each.key)
  resource_group_name   = azurerm_resource_group.main.name
  private_dns_zone_name = each.value.name
  virtual_network_id    = azurerm_virtual_network.main[0].id
  registration_enabled  = false
}

resource "azurerm_private_endpoint" "data" {
  for_each            = var.private_networking ? local.private_endpoint_specs : {}
  name                = format("pe-%s-%s", var.environment_name, replace(each.key, "_", "-"))
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  subnet_id           = azurerm_subnet.private_endpoints[0].id

  private_service_connection {
    name                           = format("psc-%s-%s", var.environment_name, replace(each.key, "_", "-"))
    private_connection_resource_id = each.value.resource_id
    subresource_names              = [each.value.subresource]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "default"
    private_dns_zone_ids = [azurerm_private_dns_zone.private[each.value.dns_key].id]
  }

  depends_on = [azurerm_private_dns_zone_virtual_network_link.private]
}
