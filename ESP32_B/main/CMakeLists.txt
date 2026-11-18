idf_component_register(SRCS "main.c" "link.c" "station.c" "jsonmini.c" "tcpframe.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_wifi esp_netif esp_event nvs_flash lwip esp_timer esp_driver_gpio)
