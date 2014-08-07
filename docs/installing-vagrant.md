## Installing using Vagrant

When developing ObrBot, it is recommended that you run the bot inside a Vagrant VM. This guarantees that everyone developing ObrBot will have an identical working environment.

#### Manual Download

Download ObrBot from [https://github.com/daboross/obrbot/archive/master.zip](https://github.com/daboross/obrbot/archive/master.zip) and unzip it.
```
curl -Ls https://github.com/daboross/obrbot/archive/master.zip > ObrBot.zip
unzip ObrBot.zip
cd obrbot-master
```

#### Git

Alternately, you can also clone ObrBot by using:
```
git clone https://github.com/daboross/obrbot.git
cd obrbot
```


### Setting up the Virtual Machine

First, you need to install Vagrant. See [docs.vagrantup.com](http://docs.vagrantup.com/v2/installation/index.html) for a guide on installing Vagrant.

Next, use the `vagrant up` command in the ObrBot directory. This may take a while, but when it's finished, you will have a fully installed ObrBot virtual machine.

To run the bot, connect to the virtual machine using `vagrant ssh`, then use the `start-bot` command in the ssh terminal.
