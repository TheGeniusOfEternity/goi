obj-m += vtfs.o
vtfs-y := source/vtfs.o source/http.o source/remote.o

KDIR ?= /lib/modules/$(shell uname -r)/build
ccflags-y += -Wall -g

.PHONY: all clean test

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules
	cp vtfs.ko source/vtfs.ko

test:
	python3 -B -m unittest discover -s test -p test_service.py -v
	python3 -B -m unittest discover -s test -p test_github.py -v

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
	rm -rf .cache
