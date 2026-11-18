# PC tests for ESP32 A. Needs only gcc (or clang) and make.
#   make -f test.mk       build and run the unit tests
#   make -f test.mk sim   build sim_a, ESP32 A on a PC (the gateway's end-to-end tests use it)
#   make clean
CC      ?= gcc
CFLAGS  := -std=c11 -Wall -Wextra -Werror -O1 -g -fsanitize=address,undefined -fno-sanitize-recover=all
SRC     := test_station.c ../main/station.c ../main/atr.c ../main/prodmsg.c ../main/crc16.c ../main/slip.c

run: test_station
	./test_station

test_station: $(SRC) $(wildcard ../main/*.h)
	$(CC) $(CFLAGS) -o $@ $(SRC)

sim: sim_a

sim_a: sim_a.c ../main/station.c ../main/atr.c ../main/prodmsg.c ../main/crc16.c ../main/slip.c $(wildcard ../main/*.h)
	$(CC) $(CFLAGS) -o $@ sim_a.c ../main/station.c ../main/atr.c ../main/prodmsg.c ../main/crc16.c ../main/slip.c

clean:
	rm -f test_station sim_a
