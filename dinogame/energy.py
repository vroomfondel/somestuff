from dinogame_utils import *

def energizer_plant_task(x_off, y_off, width, height, harvest_before=False, allow_watering=True):
    y_up = False

    petals = []
    posxy = []

    y_up = not y_up
    fromy = y_off
    toy = y_off + height
    step = 1

    if not y_up:
        fromy = y_off + height - 1
        toy = y_off - 1
        step = -1

    x_up = False
    for y in range(fromy, toy, step):
        x_up = not x_up
        fromx = x_off
        tox = x_off + width
        step = 1

        if not x_up:
            fromx = x_off + width - 1
            tox = x_off - 1
            step = -1

        for x in range(fromx, tox, step):
            move_to(x, y)

            if harvest_before and can_harvest():
                harvest()

            if plant_me(Entities.Sunflower, False, allow_watering):
                me = measure()
                if me == None:
                    me = -1
                petals.append(me)
                posxy.append((x, y))

    return petals, posxy


def harvest_with_target_droned(targets):
    available_drones = min(len(targets), max_drones() - num_drones())

    per_drone = len(targets) // available_drones
    remainder = len(targets)
    to_spread = len(targets) % available_drones

    to_dispatch = len(targets)
    dispatched = 0

    ret = []
    off = 0
    for i in range(0, available_drones):
        end = min(off + remainder, off + per_drone)
        if to_spread > 0:
            end += 1
            to_spread -= 1

        # if i+1 >= available_drones:
        #	end = off + remainder

        myjob = targets[off:end]

        # set new _off
        off = end

        dispatched += len(myjob)

        remainder -= per_drone

        def harvest_task():
            harvest_count = 0
            for point in myjob:
                x, y = point
                move_to(x, y)
                harvest()
                harvest_count += 1

            return harvest_count

        droned = spawn_drone(harvest_task)
        ret.append(droned)

    dispatch_diff = to_dispatch - dispatched
    return ret


def energizer(height, maxloops=None, width=1, harvest_before=False, extra_plant_drones=3, allow_watering=True):
    ws = get_world_size()
    start_x = ws - width
    start_y = 0

    per_drone_width = None
    per_drone_height = None

    # TODO check if drones should go horizontal or vertical
    if extra_plant_drones != None and extra_plant_drones > 0:
        per_drone_width = width  # width // (extra_plant_drones+1)
        per_drone_height = height // (extra_plant_drones + 1)

    loopcounter = 0
    y_up = False

    while (True):
        petals = []
        posxy = []
        plant_drones = None

        if per_drone_height != None:
            plant_drones = []

            for di in range(0, extra_plant_drones):
                my_x_off = start_x
                my_y_off = start_y + di * per_drone_height
                my_width = per_drone_width
                my_height = per_drone_height

                def my_planter():
                    return energizer_plant_task(my_x_off, my_y_off, my_width, my_height, harvest_before, allow_watering)

                plant_drone = spawn_drone(my_planter)
                plant_drones.append(plant_drone)

            # remainder
            my_x_off = start_x
            my_y_off = start_y + extra_plant_drones * per_drone_height
            my_width = per_drone_width
            my_height = start_y + height - my_y_off

            petals, posxy = energizer_plant_task(my_x_off, my_y_off, my_width, my_height, harvest_before,
                                                 allow_watering)

            # wait for other drones and combine stuff
            for dm in plant_drones:
                my_petals, my_posxy = wait_for(dm)

                for k in range(0, len(my_petals)):
                    petals.append(my_petals[k])
                    posxy.append(my_posxy[k])

        else:
            petals, posxy = energizer_plant_task(start_x, start_y, width, ws, harvest_before, allow_watering)

        buckets = {}
        for i in range(7, 16):
            buckets[i] = []

        # sort_two_arrays_by_first_array(petals, posxy)
        for i in range(0, len(petals)):
            point = posxy[i]
            count = petals[i]

            buckets[count].append(point)

        move_to(ws // 2, ws // 2)

        ll = len(posxy)

        prev_petals = -1
        petals_to_harvest = []
        mydrones = []

        toharvest_remainder = len(petals)
        harvested = 0

        # for i in range(ll-1, -1, -1):
        for count_index in range(15, 6, -1):
            bucket = buckets[count_index]

            mydrones = harvest_with_target_droned(bucket)
            toharvest_remainder -= len(bucket)

            for dm in mydrones:
                harvested += wait_for(dm)

            mydrones = []

        harvest_diff = len(petals) - harvested

        loopcounter = loopcounter + 1

        if maxloops != None and loopcounter >= maxloops:
            break