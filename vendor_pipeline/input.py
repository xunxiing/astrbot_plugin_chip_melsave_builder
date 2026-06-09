trigger = INPUT("Trigger", 2)
hit_entity = INPUT("HitEntity", 1)

if __name__ == "__main__":
    is_active = trigger["OUTPUT"] > 0.5
    
    zero_vel = Combine(0.0, 0.0, 0.0, 0.0)
    zero_ang = 0.0
    
    curr_vel = Velocity(hit_entity["OUTPUT"])
    curr_ang = AngularVelocity(hit_entity["OUTPUT"])
    
    if is_active:
        final_vel = zero_vel["Vector"]
        final_ang = zero_ang
    else:
        final_vel = curr_vel["Velocity"]
        final_ang = curr_ang["Angular Velocity"]
        
    Velocity(hit_entity["OUTPUT"], final_vel)
    AngularVelocity(hit_entity["OUTPUT"], final_ang)