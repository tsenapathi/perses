import sys
import simtk.openmm as openmm
import simtk.openmm.app as app
import simtk.unit as unit
import mdtraj as md
import numpy as np
import copy
import enum
from io import StringIO
import lxml.etree as etree
from openmmtools.constants import ONE_4PI_EPS0

InteractionGroup = enum.Enum("InteractionGroup", ['unique_old', 'unique_new', 'core', 'environment'])

class HybridTopologyFactory(object):
    """
    This class generates a hybrid topology based on a perses topology proposal. This class treats atoms
    in the resulting hybrid system as being from one of four classes:

    unique_old_atom : these atoms are not mapped and only present in the old system. Their interactions will be on for
        lambda=0, off for lambda=1
    unique_new_atom : these atoms are not mapped and only present in the new system. Their interactions will be off
        for lambda=0, on for lambda=1
    core_atom : these atoms are mapped, and are part of a residue that is changing. Their interactions will be those
        corresponding to the old system at lambda=0, and those corresponding to the new system at lambda=1
    environment_atom : these atoms are mapped, and are not part of a changing residue. Their interactions are always
        on and are alchemically unmodified.

    Properties
    ----------
    hybrid_system : openmm.System
        The hybrid system for simulation
    new_to_hybrid_atom_map : dict of int : int
        The mapping of new system atoms to hybrid atoms
    old_to_hybrid_atom_map : dict of int : int
        The mapping of old system atoms to hybrid atoms
    hybrid_positions : [n, 3] np.ndarray
        The positions of the hybrid system
    hybrid_topology : mdtraj.Topology
        The topology of the hybrid system
    omm_hybrid_topology : openmm.app.Topology
        The OpenMM topology object corresponding to the hybrid system

    .. warning :: This API is experimental and subject to change.

    """

    _known_forces = {'HarmonicBondForce', 'HarmonicAngleForce', 'PeriodicTorsionForce', 'NonbondedForce', 'MonteCarloBarostat'}

    def __init__(self, topology_proposal, current_positions, new_positions, use_dispersion_correction=False, functions=None, softcore_alpha=None, bond_softening_constant=1.0, angle_softening_constant=1.0, soften_only_new=False):
        """
        Initialize the Hybrid topology factory.

        Parameters
        ----------
        topology_proposal : perses.rjmc.topology_proposal.TopologyProposal object
            TopologyProposal object rendered by the ProposalEngine
        current_positions : [n,3] np.ndarray of float
            The positions of the "old system"
        new_positions : [m,3] np.ndarray of float
            The positions of the "new system"
        use_dispersion_correction : bool, default False
            Whether to use the long range correction in the custom sterics force. This is very expensive for NCMC.
        functions : dict, default None
            Alchemical functions that determine how each force is scaled with lambda. The keys must be strings with
            names beginning with lambda_ and ending with each of bonds, angles, torsions, sterics, electrostatics.
            If functions is none, then the integrator will need to set each of these and parameter derivatives will be unavailable.
            If functions is not None, all lambdas must be specified.
        softcore_alpha: float, default None
            "alpha" parameter of softcore sterics. If None is provided, value will be set to 0.5
        bond_softening_constant : float
            For bonds between unique atoms and unique-core atoms, soften the force constant at the "dummy" endpoint by this factor.
            If 1.0, do not soften
        angle_softening_constant : float
            For bonds between unique atoms and unique-core atoms, soften the force constant at the "dummy" endpoint by this factor.
            If 1.0, do not soften

        .. todo :: Document how positions for hybrid system are constructed

        """
        self._topology_proposal = topology_proposal
        self._old_system = copy.deepcopy(topology_proposal.old_system)
        self._new_system = copy.deepcopy(topology_proposal.new_system)
        self._old_to_hybrid_map = {}
        self._new_to_hybrid_map = {}
        self._hybrid_system_forces = dict()
        self._old_positions = current_positions
        self._new_positions = new_positions
        self._soften_only_new = soften_only_new

        if bond_softening_constant != 1.0:
            self._bond_softening_constant = bond_softening_constant
            self._soften_bonds = True
        else:
            self._soften_bonds = False

        if angle_softening_constant != 1.0:
            self._angle_softening_constant = angle_softening_constant
            self._soften_angles = True
        else:
            self._soften_angles = False

        self._use_dispersion_correction = use_dispersion_correction

        if softcore_alpha is None:
            self.softcore_alpha = 0.5
        else:
            # TODO: Check that softcore_alpha is in a valid range
            self.softcore_alpha = softcore_alpha


        if functions:
            self._functions = functions
            self._has_functions = True
        else:
            self._has_functions = False

        #prepare dicts of forces, which will be useful later
        # TODO: Store this as self._system_forces[name], name in ('old', 'new', 'hybrid') for compactness
        self._old_system_forces = {type(force).__name__ : force for force in self._old_system.getForces()}
        self._new_system_forces = {type(force).__name__ : force for force in self._new_system.getForces()}

        #check that there are no unknown forces in the new and old systems:
        for system_name in ('old', 'new'):
            force_names = getattr(self, '_{}_system_forces'.format(system_name)).keys()
            unknown_forces = set(force_names) - set(self._known_forces)
            if len(unknown_forces) > 0:
                raise ValueError("Unkown forces {} encountered in {} system" % (unknown_forces, system_name))

        #get and store the nonbonded method from the system:
        self._nonbonded_method = self._old_system_forces['NonbondedForce'].getNonbondedMethod()

        #start by creating an empty system. This will become the hybrid system.
        self._hybrid_system = openmm.System()

        #begin by copying all particles in the old system to the hybrid system. Note that this does not copy the
        #interactions. It does, however, copy the particle masses. In general, hybrid index and old index should be
        #the same.
        # TODO: Refactor this into self._add_particles()
        for particle_idx in range(self._topology_proposal.n_atoms_old):
            particle_mass = self._old_system.getParticleMass(particle_idx)
            hybrid_idx = self._hybrid_system.addParticle(particle_mass)
            self._old_to_hybrid_map[particle_idx] = hybrid_idx

            #If the particle index in question is mapped, make sure to add it to the new to hybrid map as well.
            if particle_idx in self._topology_proposal.old_to_new_atom_map.keys():
                particle_index_in_new_system = self._topology_proposal.old_to_new_atom_map[particle_idx]
                self._new_to_hybrid_map[particle_index_in_new_system] = hybrid_idx

        #Next, add the remaining unique atoms from the new system to the hybrid system and map accordingly.
        #As before, this does not copy interactions, only particle indices and masses.
        for particle_idx in self._topology_proposal.unique_new_atoms:
            particle_mass = self._new_system.getParticleMass(particle_idx)
            hybrid_idx = self._hybrid_system.addParticle(particle_mass)
            self._new_to_hybrid_map[particle_idx] = hybrid_idx

        #check that if there is a barostat in the original system, it is added to the hybrid.
        #We copy the barostat from the old system.
        if "MonteCarloBarostat" in self._old_system_forces.keys():
            barostat = copy.deepcopy(self._old_system_forces["MonteCarloBarostat"])
            self._hybrid_system.addForce(barostat)

        #Copy over the box vectors:
        box_vectors = self._old_system.getDefaultPeriodicBoxVectors()
        self._hybrid_system.setDefaultPeriodicBoxVectors(*box_vectors)

        #assign atoms to one of the classes described in the class docstring
        self._atom_classes = self._determine_atom_classes()

        #create the opposite atom maps for use in nonbonded force processing
        self._hybrid_to_old_map = {value : key for key, value in self._old_to_hybrid_map.items()}
        self._hybrid_to_new_map = {value : key for key, value in self._new_to_hybrid_map.items()}

        #construct dictionary of exceptions in old and new systems
        self._old_system_exceptions = self._generate_dict_from_exceptions(self._old_system_forces['NonbondedForce'])
        self._new_system_exceptions = self._generate_dict_from_exceptions(self._new_system_forces['NonbondedForce'])

        #copy constraints, checking to make sure they are not changing
        self._handle_constraints()

        #copy over relevant virtual sites
        self._handle_virtual_sites()

        #call each of the methods to add the corresponding force terms and prepare the forces:
        self._add_bond_force_terms()
        self._add_angle_force_terms()
        self._add_torsion_force_terms()
        self._add_nonbonded_force_terms()

        #call each force preparation method to generate the actual interactions that we need:
        self.handle_harmonic_bonds()
        self.handle_harmonic_angles()
        self.handle_periodic_torsion_force()
        self.handle_nonbonded()

        #get positions for the hybrid
        self._hybrid_positions = self._compute_hybrid_positions()

        #generate the topology representation
        self._hybrid_topology = self._create_topology()

    def _handle_virtual_sites(self):
        """
        Ensure that all virtual sites in old and new system are copied over to the hybrid system. Note that we do not
        support virtual sites in the changing region.
        """
        for system_name in ('old', 'new'):
            system = getattr(self._topology_proposal, '{}_system'.format(system_name))
            hybrid_atom_map = getattr(self, '_{}_to_hybrid_map'.format(system_name))

            # Loop through virtual sites
            for particle_idx in range(system.getNumParticles()):
                if system.isVirtualSite(particle_idx):
                    # If it's a virtual site, make sure it is not in the unique or core atoms, since this is currently unsupported
                    hybrid_idx = hybrid_atom_map[particle_idx]
                    if hybrid_idx not in self._atom_classes['environment_atoms']:
                        raise Exception("Virtual sites in changing residue are unsupported.")
                    else:
                        virtual_site = system.getVirtualSite(particle_idx)
                        self._hybrid_system.setVirtualSite(hybrid_idx, virtual_site)

    def _get_core_atoms(self):
        """
        Determine which atoms in the old system are part of the "core" class.

        All necessary information is contained in the topology proposal passed to the constructor.

        Returns
        -------
        core_atoms : set of int
            The set of atoms (hybrid topology indexed) that are core atoms.
        environment_atoms : set of int
            The set of atoms (hybrid topology indexed) that are environment atoms.

        .. todo ::

           Overhaul this method and methods it calls by instead having this class accept
           an alchemical atom set that denotes atoms not in the environment. The core would
           then be very easy to figure out.

        """

        #In order to be either a core or environment atom, the atom must be mapped.
        mapped_old_atoms_set = set(self._topology_proposal.old_to_new_atom_map.keys())
        mapped_new_atoms_set = set(self._topology_proposal.old_to_new_atom_map.values())
        mapped_hybrid_atoms_set = {self._old_to_hybrid_map[atom_idx] for atom_idx in mapped_old_atoms_set}

        #create sets for set arithmetic
        unique_old_set = set(self._topology_proposal.unique_old_atoms)
        unique_new_set = set(self._topology_proposal.unique_new_atoms)

        #we derive core atoms from the old topology:
        core_atoms_from_old = self._determine_core_atoms_in_topology(self._topology_proposal.old_topology,
                                                                     unique_old_set, mapped_old_atoms_set,
                                                                     self._old_to_hybrid_map)

        #we also derive core atoms from the new topology:
        core_atoms_from_new = self._determine_core_atoms_in_topology(self._topology_proposal.new_topology,
                                                                     unique_new_set, mapped_new_atoms_set,
                                                                     self._new_to_hybrid_map)

        #The union of the two will give the core atoms that can result from either new or old topology
        # TODO: Shouldn't these sets be the same?
        total_core_atoms = core_atoms_from_old.union(core_atoms_from_new)

        #as a side effect, we can now compute the environment atom indices too, by subtracting the core indices
        #from the mapped atom set (since any atom that is mapped but not core is environment)
        environment_atoms = mapped_hybrid_atoms_set.difference(total_core_atoms)

        return total_core_atoms, environment_atoms

    def _determine_core_atoms_in_topology(self, topology, unique_atoms, mapped_atoms, hybrid_map):
        """
        Given a topology and its corresponding unique and mapped atoms, return the set of atom indices in the
        hybrid system which would belong to the "core" atom class

        Parameters
        ----------
        topology : simtk.openmm.app.Topology
            An OpenMM topology representing a system of interest
        unique_atoms : set of int
            A set of atoms that are unique to this topology
        mapped_atoms : set of int
            A set of atoms that are mapped to another topology

        Returns
        -------
        core_atoms : set of int
            set of core atom indices in hybrid topology
        """
        core_atoms = set()

        #loop through the residues to look for ones with unique atoms
        for residue in topology.residues():
            atom_indices_old_system = {atom.index for atom in residue.atoms()}

            #if the residue contains an atom index that is unique, then the residue is changing.
            #We determine this by checking if the atom indices of the residue have any intersection with the unique atoms
            if len(atom_indices_old_system.intersection(unique_atoms)) > 0:
                #we can add the atoms in this residue which are mapped to the core_atoms set:
                for atom_index in atom_indices_old_system:
                    if atom_index in mapped_atoms:
                        #we specifically want to add the hybrid atom.
                        hybrid_index = hybrid_map[atom_index]
                        core_atoms.add(hybrid_index)

        return core_atoms

    def _determine_atom_classes(self):
        """
        This method determines whether each atom belongs to unique old, unique new, core, or environment, as defined above.
        All the information required is contained in the TopologyProposal passed to the constructor. All indices are
        indices in the hybrid system.

        Returns
        -------
        atom_classes : dict of list
            A dictionary of the form {'core' :core_list} etc.
        """
        atom_classes = {'unique_old_atoms' : set(), 'unique_new_atoms' : set(), 'core_atoms' : set(), 'environment_atoms' : set()}

        #first, find the unique old atoms, as this is the most straightforward:
        for atom_idx in self._topology_proposal.unique_old_atoms:
            hybrid_idx = self._old_to_hybrid_map[atom_idx]
            atom_classes['unique_old_atoms'].add(hybrid_idx)

        #Then the unique new atoms (this is substantially the same as above)
        for atom_idx in self._topology_proposal.unique_new_atoms:
            hybrid_idx = self._new_to_hybrid_map[atom_idx]
            atom_classes['unique_new_atoms'].add(hybrid_idx)

        core_atoms, environment_atoms = self._get_core_atoms()

        atom_classes['core_atoms'] = core_atoms
        atom_classes['environment_atoms'] = environment_atoms

        return atom_classes

    def _translate_nonbonded_method_to_custom(self, standard_nonbonded_method):
        """
        Utility function to translate the nonbonded method enum from the standard nonbonded force to the custom version
       `CutoffPeriodic`, `PME`, and `Ewald` all become `CutoffPeriodic`; `NoCutoff` becomes `NoCutoff`; `CutoffNonPeriodic` becomes `CutoffNonPeriodic`
        Parameters
        ----------
        standard_nonbonded_method : openmm.NonbondedForce.NonbondedMethod
            the nonbonded method of the standard force

        Returns
        -------
        custom_nonbonded_method : openmm.CustomNonbondedForce.NonbondedMethod
            the nonbonded method for the equivalent customnonbonded force
        """
        if standard_nonbonded_method in [openmm.NonbondedForce.CutoffPeriodic, openmm.NonbondedForce.PME, openmm.NonbondedForce.Ewald]:
            return openmm.CustomNonbondedForce.CutoffPeriodic
        elif standard_nonbonded_method == openmm.NonbondedForce.NoCutoff:
            return openmm.CustomNonbondedForce.NoCutoff
        elif standard_nonbonded_method == openmm.NonbondedForce.CutoffNonPeriodic:
            return openmm.CustomNonbondedForce.CutoffNonPeriodic
        else:
            raise NotImplementedError("This nonbonded method is not supported.")

    def _handle_constraints(self):
        """
        This method adds relevant constraints from the old and new systems.

        First, all constraints from the old systenm are added.
        Then, constraints to atoms unique to the new system are added.

        """
        constraint_lengths = dict() # lengths of constraints already added
        for system_name in ('old', 'new'):
            system = getattr(self._topology_proposal, '{}_system'.format(system_name))
            hybrid_map = getattr(self, '_{}_to_hybrid_map'.format(system_name))
            for constraint_idx in range(system.getNumConstraints()):
                atom1, atom2, length = system.getConstraintParameters(constraint_idx)
                hybrid_atoms = tuple(sorted([hybrid_map[atom1], hybrid_map[atom2]]))
                if hybrid_atoms not in constraint_lengths.keys():
                    self._hybrid_system.addConstraint(hybrid_atoms[0], hybrid_atoms[1], length)
                    constraint_lengths[hybrid_atoms] = length
                else:
                    # TODO: We can skip this if we have already checked for constraints changing lengths
                    if constraint_lengths[hybrid_atoms] != length:
                        raise Exception('Constraint length is changing for atoms {} in hybrid system: old {} new {}'.format(hybrid_atoms, constraint_lengths[hybrid_atoms], length))
    def _determine_interaction_group(self, atoms_in_interaction):
        """
        This method determines which interaction group the interaction should fall under. There are four groups:

        Those involving unique old atoms: any interaction involving unique old atoms should be completely on at lambda=0
            and completely off at lambda=1

        Those involving unique new atoms: any interaction involving unique new atoms should be completely off at lambda=0
            and completely on at lambda=1

        Those involving core atoms and/or environment atoms: These interactions change their type, and should be the old
            character at lambda=0, and the new character at lambda=1

        Those involving only environment atoms: These interactions are unmodified.

        Parameters
        ----------
        atoms_in_interaction : list of int
            List of (hybrid) indices of the atoms in this interaction

        Returns
        -------
        interaction_group : InteractionGroup enum
            The group to which this interaction should be assigned
        """
        #make the interaction list a set to facilitate operations
        atom_interaction_set = set(atoms_in_interaction)

        #check if the interaction contains unique old atoms
        if len(atom_interaction_set.intersection(self._atom_classes['unique_old_atoms'])) > 0:
            return InteractionGroup.unique_old

        #Do the same for new atoms
        elif len(atom_interaction_set.intersection(self._atom_classes['unique_new_atoms'])) > 0:
            return InteractionGroup.unique_new

        #if the interaction set is a strict subset of the environment atoms, then it is in the environment group
        #and should not be alchemically modified at all.
        elif atom_interaction_set.issubset(self._atom_classes['environment_atoms']):
            return InteractionGroup.environment

        #having covered the cases of all-environment, unique old-containing, and unique-new-containing, anything else
        #should belong to the last class--contains core atoms but not any unique atoms.
        else:
            return InteractionGroup.core

    def _add_bond_force_terms(self):
        """
        This function adds the appropriate bond forces to the system (according to groups defined above). Note that it
        does _not_ add the particles to the force. It only adds the force to facilitate another method adding the
        particles to the force.
        """
        core_energy_expression = '(K/2)*(r-length)^2;'
        core_energy_expression += 'K = (1-lambda_bonds)*K1 + lambda_bonds*K2;' # linearly interpolate spring constant
        core_energy_expression += 'length = (1-lambda_bonds)*length1 + lambda_bonds*length2;' # linearly interpolate bond length
        if self._has_functions:
            try:
                core_energy_expression += 'lambda_bonds = ' + self._functions['lambda_bonds']
            except KeyError as e:
                print("Functions were provided, but no term was provided for the bonds")
                raise e

        #create the force and add the relevant parameters
        custom_core_force = openmm.CustomBondForce(core_energy_expression)
        custom_core_force.addPerBondParameter('length1') # old bond length
        custom_core_force.addPerBondParameter('K1') # old spring constant
        custom_core_force.addPerBondParameter('length2') # new bond length
        custom_core_force.addPerBondParameter('K2') #new spring constant

        if self._has_functions:
            custom_core_force.addGlobalParameter('lambda', 0.0)
            custom_core_force.addEnergyParameterDerivative('lambda')
        else:
            custom_core_force.addGlobalParameter('lambda_bonds', 0.0)

        self._hybrid_system.addForce(custom_core_force)
        self._hybrid_system_forces['core_bond_force'] = custom_core_force

        #add a bond force for environment and unique atoms (bonds are never scaled for these):
        standard_bond_force = openmm.HarmonicBondForce()
        self._hybrid_system.addForce(standard_bond_force)
        self._hybrid_system_forces['standard_bond_force'] = standard_bond_force

    def _add_angle_force_terms(self):
        """
        This function adds the appropriate angle force terms to the hybrid system. It does not add particles
        or parameters to the force; this is done elsewhere.
        """
        energy_expression  = '(K/2)*(theta-theta0)^2;'
        energy_expression += 'K = (1.0-lambda_angles)*K_1 + lambda_angles*K_2;' # linearly interpolate spring constant
        energy_expression += 'theta0 = (1.0-lambda_angles)*theta0_1 + lambda_angles*theta0_2;' # linearly interpolate equilibrium angle
        if self._has_functions:
            try:
                energy_expression += 'lambda_angles = ' + self._functions['lambda_angles']
            except KeyError as e:
                print("Functions were provided, but no term was provided for the angles")
                raise e

        #create the force and add relevant parameters
        custom_core_force = openmm.CustomAngleForce(energy_expression)
        custom_core_force.addPerAngleParameter('theta0_1') # molecule1 equilibrium angle
        custom_core_force.addPerAngleParameter('K_1') # molecule1 spring constant
        custom_core_force.addPerAngleParameter('theta0_2') # molecule2 equilibrium angle
        custom_core_force.addPerAngleParameter('K_2') # molecule2 spring constant

        if self._has_functions:
            custom_core_force.addGlobalParameter('lambda', 0.0)
            custom_core_force.addEnergyParameterDerivative('lambda')
        else:
            custom_core_force.addGlobalParameter('lambda_angles', 0.0)


        #add the force to the system and the force dict.
        self._hybrid_system.addForce(custom_core_force)
        self._hybrid_system_forces['core_angle_force'] = custom_core_force

        #add an angle term for environment/unique interactions--these are never scaled
        standard_angle_force = openmm.HarmonicAngleForce()
        self._hybrid_system.addForce(standard_angle_force)
        self._hybrid_system_forces['standard_angle_force'] = standard_angle_force

    def _add_torsion_force_terms(self):
        """
        This function adds the appropriate PeriodicTorsionForce terms to the system. Core torsions are interpolated,
        while environment and unique torsions are always on.
        """
        energy_expression  = '(1-lambda_torsions)*U1 + lambda_torsions*U2;'
        energy_expression += 'U1 = K1*(1+cos(periodicity1*theta-phase1));'
        energy_expression += 'U2 = K2*(1+cos(periodicity2*theta-phase2));'

        if self._has_functions:
            try:
                energy_expression += 'lambda_torsions = ' + self._functions['lambda_torsions']
            except KeyError as e:
                print("Functions were provided, but no term was provided for torsions")
                raise e


        #create the force and add the relevant parameters
        custom_core_force = openmm.CustomTorsionForce(energy_expression)
        custom_core_force.addPerTorsionParameter('periodicity1') # molecule1 periodicity
        custom_core_force.addPerTorsionParameter('phase1') # molecule1 phase
        custom_core_force.addPerTorsionParameter('K1') # molecule1 spring constant
        custom_core_force.addPerTorsionParameter('periodicity2') # molecule2 periodicity
        custom_core_force.addPerTorsionParameter('phase2') # molecule2 phase
        custom_core_force.addPerTorsionParameter('K2') # molecule2 spring constant

        if self._has_functions:
            custom_core_force.addGlobalParameter('lambda', 0.0)
            custom_core_force.addEnergyParameterDerivative('lambda')
        else:
            custom_core_force.addGlobalParameter('lambda_torsions', 0.0)

        #add the force to the system
        self._hybrid_system.addForce(custom_core_force)
        self._hybrid_system_forces['core_torsion_force'] = custom_core_force

        #create and add the torsion term for unique/environment atoms
        standard_torsion_force = openmm.PeriodicTorsionForce()
        self._hybrid_system.addForce(standard_torsion_force)
        self._hybrid_system_forces['standard_torsion_force'] = standard_torsion_force

    def _add_nonbonded_force_terms(self):
        """
        Add the nonbonded force terms to the hybrid system. Note that as with the other forces,
        this method does not add any interactions. It only sets up the forces.

        Parameters
        ----------
        nonbonded_method : int
            One of the openmm.NonbondedForce nonbonded methods.
        """

        #Add a regular nonbonded force for all interactions that are not changing.
        standard_nonbonded_force = openmm.NonbondedForce()
        self._hybrid_system.addForce(standard_nonbonded_force)
        self._hybrid_system_forces['standard_nonbonded_force'] = standard_nonbonded_force

        # Create a CustomNonbondedForce to handle alchemically interpolated nonbonded parameters.
        # Select functional form based on nonbonded method.
        if self._nonbonded_method in [openmm.NonbondedForce.NoCutoff]:
            sterics_energy_expression = self._nonbonded_custom_nocutoff()
        elif self._nonbonded_method in [openmm.NonbondedForce.CutoffPeriodic, openmm.NonbondedForce.CutoffNonPeriodic]:
            epsilon_solvent = self._old_system_forces['NonbondedForce'].getReactionFieldDielectric()
            r_cutoff = self._old_system_forces['NonbondedForce'].getCutoffDistance()
            sterics_energy_expression = self._nonbonded_custom_cutoff(epsilon_solvent, r_cutoff)
            standard_nonbonded_force.setReactionFieldDielectric(epsilon_solvent)
            standard_nonbonded_force.setCutoffDistance(r_cutoff)
        elif self._nonbonded_method in [openmm.NonbondedForce.PME, openmm.NonbondedForce.Ewald]:
            [alpha_ewald, nx, ny, nz] = self._old_system_forces['NonbondedForce'].getPMEParameters()
            delta = self._old_system_forces['NonbondedForce'].getEwaldErrorTolerance()
            r_cutoff = self._old_system_forces['NonbondedForce'].getCutoffDistance()
            sterics_energy_expression = self._nonbonded_custom_ewald(alpha_ewald, delta, r_cutoff)
            standard_nonbonded_force.setPMEParameters(alpha_ewald, nx, ny, nz)
            standard_nonbonded_force.setEwaldErrorTolerance(delta)
            standard_nonbonded_force.setCutoffDistance(r_cutoff)
        else:
            raise Exception("Nonbonded method %s not supported yet." % str(self._nonbonded_method))

        standard_nonbonded_force.setNonbondedMethod(self._nonbonded_method)
        sterics_energy_expression += self._nonbonded_custom_sterics_common()

        sterics_mixing_rules = self._nonbonded_custom_mixing_rules()

        custom_nonbonded_method = self._translate_nonbonded_method_to_custom(self._nonbonded_method)

        total_sterics_energy = "U_sterics;" + sterics_energy_expression + sterics_mixing_rules
        if self._has_functions:
            try:
                total_sterics_energy += 'lambda_sterics  = ' + self._functions['lambda_sterics']
            except KeyError as e:
                print("Functions were provided, but there is no entry for sterics")
                raise e

        sterics_custom_nonbonded_force = openmm.CustomNonbondedForce(total_sterics_energy)
        sterics_custom_nonbonded_force.addGlobalParameter("softcore_alpha", self.softcore_alpha)
        sterics_custom_nonbonded_force.addPerParticleParameter("sigmaA") # Lennard-Jones sigma initial
        sterics_custom_nonbonded_force.addPerParticleParameter("epsilonA") # Lennard-Jones epsilon initial
        sterics_custom_nonbonded_force.addPerParticleParameter("sigmaB") # Lennard-Jones sigma final
        sterics_custom_nonbonded_force.addPerParticleParameter("epsilonB") # Lennard-Jones epsilon final
        sterics_custom_nonbonded_force.addPerParticleParameter("unique_old") # 1 = hybrid old atom, 0 otherwise
        sterics_custom_nonbonded_force.addPerParticleParameter("unique_new") # 1 = hybrid new atom, 0 otherwise

        if self._has_functions:
            sterics_custom_nonbonded_force.addGlobalParameter('lambda', 0.0)
            sterics_custom_nonbonded_force.addEnergyParameterDerivative('lambda')
        else:
            sterics_custom_nonbonded_force.addGlobalParameter("lambda_sterics_core", 0.0)
            sterics_custom_nonbonded_force.addGlobalParameter("lambda_electrostatics_core", 0.0)
            sterics_custom_nonbonded_force.addGlobalParameter("lambda_sterics_insert", 0.0)
            sterics_custom_nonbonded_force.addGlobalParameter("lambda_sterics_delete", 0.0)


        sterics_custom_nonbonded_force.setNonbondedMethod(custom_nonbonded_method)


        self._hybrid_system.addForce(sterics_custom_nonbonded_force)
        self._hybrid_system_forces['core_sterics_force'] = sterics_custom_nonbonded_force


        #set the use of dispersion correction to be the same between the new nonbonded force and the old one:
        if self._old_system_forces['NonbondedForce'].getUseDispersionCorrection():
            self._hybrid_system_forces['standard_nonbonded_force'].setUseDispersionCorrection(True)
            if self._use_dispersion_correction:
                sterics_custom_nonbonded_force.setUseLongRangeCorrection(True)

        if self._old_system_forces['NonbondedForce'].getUseSwitchingFunction():
            switching_distance = self._old_system_forces['NonbondedForce'].getSwitchingDistance()
            standard_nonbonded_force.setUseSwitchingFunction(True)
            standard_nonbonded_force.setSwitchingDistance(switching_distance)
            sterics_custom_nonbonded_force.setUseSwitchingFunction(True)
            sterics_custom_nonbonded_force.setSwitchingDistance(switching_distance)
        else:
            standard_nonbonded_force.setUseSwitchingFunction(False)
            sterics_custom_nonbonded_force.setUseSwitchingFunction(False)

    def _nonbonded_custom_sterics_common(self):
        """
        Get a custom sterics expression using amber softcore expression

        Returns
        -------
        sterics_addition : str
            The common softcore sterics energy expression
        """
        sterics_addition = "epsilon = (1-lambda_sterics)*epsilonA + lambda_sterics*epsilonB;" #interpolation
        sterics_addition += "reff_sterics = sigma*((softcore_alpha*lambda_alpha + (r/sigma)^6))^(1/6);" # effective softcore distance for sterics
        sterics_addition += "sigma = (1-lambda_sterics)*sigmaA + lambda_sterics*sigmaB;"


        sterics_addition += "lambda_alpha = new_interaction*(1-lambda_sterics_insert) + old_interaction*lambda_sterics_delete;"
        sterics_addition += "lambda_sterics = core_interaction*lambda_sterics_core + new_interaction*lambda_sterics_insert + old_interaction*lambda_sterics_delete;"
        sterics_addition += "core_interaction = delta(unique_old1+unique_old2+unique_new1+unique_new2);new_interaction = max(unique_new1, unique_new2);old_interaction = max(unique_old1, unique_old2);"

        return sterics_addition

    def _nonbonded_custom_nocutoff(self):
        """
        Get a part of the nonbonded energy expression when there is no cutoff.

        Returns
        -------
        sterics_energy_expression : str
            The energy expression for U_sterics
        electrostatics_energy_expression : str
            The energy expression for electrostatics
        """
        # soft-core Lennard-Jones
        sterics_energy_expression = "U_sterics = 4*epsilon*x*(x-1.0); x = (sigma/reff_sterics)^6;"
        return sterics_energy_expression

    def _nonbonded_custom_cutoff(self, epsilon_solvent, r_cutoff):
        """
        Get the energy expressions for sterics and electrostatics under a reaction field assumption.

        Parameters
        ----------
        epsilon_solvent : float
            The reaction field dielectric
        r_cutoff : float
            The cutoff distance

        Returns
        -------
        sterics_energy_expression : str
            The energy expression for U_sterics
        electrostatics_energy_expression : str
            The energy expression for electrostatics
        """
        # soft-core Lennard-Jones
        sterics_energy_expression = "U_sterics = 4*epsilon*x*(x-1.0); x = (sigma/reff_sterics)^6;"

        return sterics_energy_expression

    def _nonbonded_custom_ewald(self, alpha_ewald, delta, r_cutoff):
        """
        Get the energy expression for Ewald treatment.

        Parameters
        ----------
        alpha_ewald : float
            The Ewald alpha parameter
        delta : float
            The PME error tolerance
        r_cutoff : float
            The cutoff distance

        Returns
        -------
        sterics_energy_expression : str
            The energy expression for U_sterics
        electrostatics_energy_expression : str
            The energy expression for electrostatics
        """
        # soft-core Lennard-Jones
        sterics_energy_expression = "U_sterics = 4*epsilon*x*(x-1.0); x = (sigma/reff_sterics)^6;"
        if unit.is_quantity(alpha_ewald):
            alpha_ewald = alpha_ewald / alpha_ewald.in_unit_system(unit.md_unit_system).unit
        if alpha_ewald == 0.0:
            # If alpha is 0.0, alpha_ewald is computed by OpenMM from from the error tolerance.
            alpha_ewald = np.sqrt(-np.log(2*delta)) / r_cutoff
            alpha_ewald = alpha_ewald / alpha_ewald.in_unit_system(unit.md_unit_system).unit
        return sterics_energy_expression

    def _nonbonded_custom_mixing_rules(self):
        """
        Mixing rules for the custom nonbonded force.

        Returns
        -------
        sterics_mixing_rules : str
            The mixing expression for sterics
        electrostatics_mixing_rules : str
            The mixiing rules for electrostatics
        """
        # Define mixing rules.
        sterics_mixing_rules = "epsilonA = sqrt(epsilonA1*epsilonA2);" # mixing rule for epsilon
        sterics_mixing_rules += "epsilonB = sqrt(epsilonB1*epsilonB2);" # mixing rule for epsilon
        sterics_mixing_rules += "sigmaA = 0.5*(sigmaA1 + sigmaA2);" # mixing rule for sigma
        sterics_mixing_rules += "sigmaB = 0.5*(sigmaB1 + sigmaB2);" # mixing rule for sigma
        return sterics_mixing_rules

    def _find_bond_parameters(self, bond_force, index1, index2):
        """
        This is a convenience function to find bond parameters in another system given the two indices.

        Parameters
        ----------
        bond_force : openmm.HarmonicBondForce
            The bond force where the parameters should be found
        index1 : int
           Index1 (order does not matter) of the bond atoms
        index2 : int
           Index2 (order does not matter) of the bond atoms

        Returns
        -------
        bond_parameters : list
            List of relevant bond parameters
        """
        index_set = {index1, index2}
        #loop through all the bonds:
        for bond_index in range(bond_force.getNumBonds()):
            parms = bond_force.getBondParameters(bond_index)
            if index_set=={parms[0], parms[1]}:
                return parms

        return []

    def handle_harmonic_bonds(self):
        """
        This method adds the appropriate interaction for all bonds in the hybrid system. The scheme used is:

        1) If the two atoms are both in the core, then we add to the CustomBondForce and interpolate between the two
            parameters
        2) Otherwise, we add the bond to a regular bond force.
        """
        old_system_bond_force = self._old_system_forces['HarmonicBondForce']
        new_system_bond_force = self._new_system_forces['HarmonicBondForce']

        #first, loop through the old system bond forces and add relevant terms
        for bond_index in range(old_system_bond_force.getNumBonds()):
            #get each set of bond parameters
            [index1_old, index2_old, r0_old, k_old] = old_system_bond_force.getBondParameters(bond_index)

            #map the indices to the hybrid system, for which our atom classes are defined.
            index1_hybrid = self._old_to_hybrid_map[index1_old]
            index2_hybrid = self._old_to_hybrid_map[index2_old]
            index_set = {index1_hybrid, index2_hybrid}

            #now check if it is a subset of the core atoms (that is, both atoms are in the core)
            #if it is, we need to find the parameters in the old system so that we can interpolate
            if index_set.issubset(self._atom_classes['core_atoms']):
                index1_new = self._topology_proposal.old_to_new_atom_map[index1_old]
                index2_new = self._topology_proposal.old_to_new_atom_map[index2_old]
                new_bond_parameters = self._find_bond_parameters(new_system_bond_force, index1_new, index2_new)
                if not new_bond_parameters:
                    r0_new = r0_old
                    k_new = 0.0*unit.kilojoule_per_mole/unit.angstrom**2
                else:
                    [index1, index2, r0_new, k_new] = self._find_bond_parameters(new_system_bond_force, index1_new, index2_new)
                self._hybrid_system_forces['core_bond_force'].addBond(index1_hybrid, index2_hybrid,[r0_old, k_old, r0_new, k_new])

            #check if the index set is a subset of anything besides environemnt (in the case of environment, we just add the bond to the regular bond force)
            # that would mean that this bond is core-unique_old or unique_old-unique_old
            elif not index_set.issubset(self._atom_classes['environment_atoms']):

                # If we're not softening bonds, we can just add it to the regular bond force. Likewise if we are only softening new bonds
                if not self._soften_bonds or self._soften_only_new:
                    self._hybrid_system_forces['standard_bond_force'].addBond(index1_hybrid, index2_hybrid, r0_old,
                                                                              k_old)
                # Otherwise, we will need to soften one of the endpoints. For unique old atoms, the softening endpoint is at lambda =1
                else:

                    r0_new = r0_old # The bond length won't change
                    k_new = self._bond_softening_constant * k_old # We multiply the endpoint by the bond softening constant

                    # Now we add to the core bond force, since that is an alchemically-modified force.
                    self._hybrid_system_forces['core_bond_force'].addBond(index1_hybrid, index2_hybrid,
                                                                          [r0_old, k_old, r0_new, k_new])

            #otherwise, we just add the same parameters as those in the old system (these are environment atoms, and the parameters are the same)
            else:
                self._hybrid_system_forces['standard_bond_force'].addBond(index1_hybrid, index2_hybrid, r0_old, k_old)


        #now loop through the new system to get the interactions that are unique to it.
        for bond_index in range(new_system_bond_force.getNumBonds()):
            #get each set of bond parameters
            [index1_new, index2_new, r0_new, k_new] = new_system_bond_force.getBondParameters(bond_index)

            #convert indices to hybrid, since that is how we represent atom classes:
            index1_hybrid = self._new_to_hybrid_map[index1_new]
            index2_hybrid = self._new_to_hybrid_map[index2_new]
            index_set = {index1_hybrid, index2_hybrid}

            #if the intersection of this set and unique new atoms contains anything, the bond is unique to the new system and must be added
            #all other bonds in the new system have been accounted for already.
            if len(index_set.intersection(self._atom_classes['unique_new_atoms'])) > 0:

                # If we are softening bonds, we have to use the core bond force, and scale the force constant at lambda = 0:
                if self._soften_bonds:
                    r0_old = r0_new # Do not change the length
                    k_old = k_new * self._bond_softening_constant # Scale the force constant by the requested parameter

                    # Now we add to the core bond force, since that is an alchemically-modified force.
                    self._hybrid_system_forces['core_bond_force'].addBond(index1_hybrid, index2_hybrid,
                                                                          [r0_old, k_old, r0_new, k_new])

                # If we aren't softening bonds, then just add it to the standard bond force
                else:
                    self._hybrid_system_forces['standard_bond_force'].addBond(index1_hybrid, index2_hybrid, r0_new, k_new)

            #if the bond is in the core, it has probably already been added in the above loop. However, there are some circumstances
            #where it was not (closing a ring). In that case, the bond has not been added and should be added here.
            if index_set.issubset(self._atom_classes['core_atoms']):
                 if not self._find_bond_parameters(self._hybrid_system_forces['core_bond_force'], index1_hybrid, index2_hybrid):
                     r0_old = r0_new
                     k_old = 0.0*unit.kilojoule_per_mole/unit.angstrom**2
                     self._hybrid_system_forces['core_bond_force'].addBond(index1_hybrid, index2_hybrid,
                                                                           [r0_old, k_old, r0_new, k_new])


    def _find_angle_parameters(self, angle_force, indices):
        """
        Convenience function to find the angle parameters corresponding to a particular set of indices

        Parameters
        ----------
        angle_force : openmm.HarmonicAngleForce
            The force where the angle of interest may be found.
        indices : list of int
            The indices (any order) of the angle atoms
        Returns
        -------
        angle_parameters : list
            list of angle parameters
        """
        index_set = set(indices)

        #now loop through and try to find the angle:
        for angle_index in range(angle_force.getNumAngles()):
            angle_parameters = angle_force.getAngleParameters(angle_index)

            #get a set representing the angle indices
            angle_parameter_indices = set(angle_parameters[:3])

            if index_set==angle_parameter_indices:
                return angle_parameters
        return []  # return empty if no matching angle found

    def _find_torsion_parameters(self, torsion_force, indices):
        """
        Convenience function to find the torsion parameters corresponding to a particular set of indices.

        Parameters
        ----------
        torsion_force : openmm.PeriodicTorsionForce
            torsion force where the torsion of interest may be found
        indices : list of int
            The indices (any order) of the atoms of the torsion

        Returns
        -------
        torsion_parameters : list
            torsion parameters
        """
        index_set = set(indices)

        torsion_parameters_list = list()

        #now loop through and try to find the torsion:
        for torsion_index in range(torsion_force.getNumTorsions()):
            torsion_parameters = torsion_force.getTorsionParameters(torsion_index)

            #get a set representing the torsion indices:
            torsion_parameter_indices = set(torsion_parameters[:4])

            if index_set==torsion_parameter_indices:
                torsion_parameters_list.append(torsion_parameters)

        return torsion_parameters_list

    def handle_harmonic_angles(self):
        """
        This method adds the appropriate interaction for all angles in the hybrid system. The scheme used, as with bonds, is:

        1) If the three atoms are all in the core, then we add to the CustomAngleForce and interpolate between the two
            parameters
        2) Otherwise, we add the angle to a regular angle force.
        """
        old_system_angle_force = self._old_system_forces['HarmonicAngleForce']
        new_system_angle_force = self._new_system_forces['HarmonicAngleForce']

        #first, loop through all the angles in the old system to determine what to do with them. We will only use the
        #custom angle force if all atoms are part of "core." Otherwise, they are either unique to one system or never
        #change.
        for angle_index in range(old_system_angle_force.getNumAngles()):
            old_angle_parameters = old_system_angle_force.getAngleParameters(angle_index)

            #get the indices in the hybrid system
            hybrid_index_list = [self._old_to_hybrid_map[old_atomid] for old_atomid in old_angle_parameters[:3]]
            hybrid_index_set = set(hybrid_index_list)

            #if all atoms are in the core, we'll need to find the corresponding parameters in the old system and
            #interpolate
            if hybrid_index_set.issubset(self._atom_classes['core_atoms']):
                #get the new indices so we can get the new angle parameters
                new_indices = [self._topology_proposal.old_to_new_atom_map[old_atomid] for old_atomid in old_angle_parameters[:3]]
                new_angle_parameters = self._find_angle_parameters(new_system_angle_force, new_indices)
                if not new_angle_parameters:
                    new_angle_parameters = [0, 0, 0, old_angle_parameters[3], 0.0*unit.kilojoule_per_mole/unit.radian**2]

                #add to the hybrid force:
                #the parameters at indices 3 and 4 represent theta0 and k, respectively.
                hybrid_force_parameters = [old_angle_parameters[3], old_angle_parameters[4], new_angle_parameters[3], new_angle_parameters[4]]
                self._hybrid_system_forces['core_angle_force'].addAngle(hybrid_index_list[0], hybrid_index_list[1], hybrid_index_list[2], hybrid_force_parameters)

            # Check if the atoms are neither all core nor all environment, which would mean they involve unique old interactions
            elif not hybrid_index_set.issubset(self._atom_classes['environment_atoms']):

                # Check if we are softening angles, and not softening only new angles:
                if self._soften_angles and not self._soften_only_new:

                    # If we are, then we need to generate the softened parameters (at lambda=1 for old atoms)
                    # We do this by using the same equilibrium angle, and scaling the force constant at the non-interacting
                    # endpoint:
                    hybrid_force_parameters = [old_angle_parameters[3], old_angle_parameters[4], old_angle_parameters[3],
                                               self._angle_softening_constant * old_angle_parameters[4]]

                    # Add this interaction to the alchemical angle force
                    self._hybrid_system_forces['core_angle_force'].addAngle(hybrid_index_list[0], hybrid_index_list[1],
                                                                            hybrid_index_list[2],
                                                                            hybrid_force_parameters)


                # If not, we can just add this to the standard angle force
                else:
                    self._hybrid_system_forces['standard_angle_force'].addAngle(hybrid_index_list[0],
                                                                                hybrid_index_list[1],
                                                                                hybrid_index_list[2],
                                                                                old_angle_parameters[3],
                                                                                old_angle_parameters[4])
            #otherwise, only environment atoms are in this interaction, so add it to the standard angle force
            else:
                self._hybrid_system_forces['standard_angle_force'].addAngle(hybrid_index_list[0], hybrid_index_list[1],
                                                                            hybrid_index_list[2], old_angle_parameters[3],
                                                                            old_angle_parameters[4])

        #finally, loop through the new system force to add any unique new angles
        for angle_index in range(new_system_angle_force.getNumAngles()):
            new_angle_parameters = new_system_angle_force.getAngleParameters(angle_index)

            #get the indices in the hybrid system
            hybrid_index_list = [self._new_to_hybrid_map[new_atomid] for new_atomid in new_angle_parameters[:3]]
            hybrid_index_set = set(hybrid_index_list)

            #if the intersection of this hybrid set with the unique new atoms is nonempty, it must be added:
            if len(hybrid_index_set.intersection(self._atom_classes['unique_new_atoms'])) > 0:

                # Check to see if we are softening angles:
                if self._soften_angles:

                    # If so, generate the parameters for the alchemical angle by scaling the force constant at the dummy endpoint (lambda=0 for new atoms)
                    hybrid_force_parameters = [new_angle_parameters[3], new_angle_parameters[4] * self._angle_softening_constant, new_angle_parameters[3], new_angle_parameters[4]]

                    # Then add the angle to the alchemical force:
                    self._hybrid_system_forces['core_angle_force'].addAngle(hybrid_index_list[0], hybrid_index_list[1],
                                                                            hybrid_index_list[2],
                                                                            hybrid_force_parameters)
                # Otherwise, just add to the nonalchemical force
                else:
                    self._hybrid_system_forces['standard_angle_force'].addAngle(hybrid_index_list[0], hybrid_index_list[1],
                                                                            hybrid_index_list[2], new_angle_parameters[3],
                                                                            new_angle_parameters[4])

            if hybrid_index_set.issubset(self._atom_classes['core_atoms']):
                if not self._find_angle_parameters(self._hybrid_system_forces['core_angle_force'], hybrid_index_list):
                    hybrid_force_parameters = [new_angle_parameters[3], 0.0*unit.kilojoule_per_mole/unit.radian**2, new_angle_parameters[3], new_angle_parameters[4]]
                    self._hybrid_system_forces['core_angle_force'].addAngle(hybrid_index_list[0], hybrid_index_list[1],
                                                                            hybrid_index_list[2],
                                                                            hybrid_force_parameters)

    def handle_periodic_torsion_force(self):
        """
        Handle the torsions in the hybrid system in the same way as the angles and bonds.
        """
        old_system_torsion_force = self._old_system_forces['PeriodicTorsionForce']
        new_system_torsion_force = self._new_system_forces['PeriodicTorsionForce']

        #first, loop through all the torsions in the old system to determine what to do with them. We will only use the
        #custom torsion force if all atoms are part of "core." Otherwise, they are either unique to one system or never
        #change.

        #we need to keep track of what torsions we added so that we do not double count.
        added_torsions = []
        for torsion_index in range(old_system_torsion_force.getNumTorsions()):
            torsion_parameters = old_system_torsion_force.getTorsionParameters(torsion_index)

            #get the indices in the hybrid system
            hybrid_index_list = [self._old_to_hybrid_map[old_index] for old_index in torsion_parameters[:4]]
            hybrid_index_set = set(hybrid_index_list)

            #if all atoms are in the core, we'll need to find the corresponding parameters in the old system and
            #interpolate
            if hybrid_index_set.issubset(self._atom_classes['core_atoms']):
                torsion_indices = torsion_parameters[:4]

                #if we've already added these indices (they may appear >once for high periodicities)
                #then just continue to the next torsion.
                if torsion_indices in added_torsions:
                    continue
                #get the new indices so we can get the new angle parameters, as well as all old parameters of the old torsion
                #The reason we do it like this is to take care of varying periodicity between new and old system.
                torsion_parameters_list = self._find_torsion_parameters(old_system_torsion_force, torsion_indices)
                new_indices = [self._topology_proposal.old_to_new_atom_map[old_index] for old_index in torsion_indices]
                new_torsion_parameters_list = self._find_torsion_parameters(new_system_torsion_force, new_indices)

                #for old torsions, have the energy scale from full at lambda=0 to off at lambda=1
                for torsion_parameters in torsion_parameters_list:
                    hybrid_force_parameters = [torsion_parameters[4], torsion_parameters[5], torsion_parameters[6], 0.0, 0.0, 0.0]
                    self._hybrid_system_forces['core_torsion_force'].addTorsion(hybrid_index_list[0], hybrid_index_list[1], hybrid_index_list[2], hybrid_index_list[3], hybrid_force_parameters)

                #for new torsions, have the energy scale from 0 at lambda=0 to full at lambda=1
                for torsion_parameters in new_torsion_parameters_list:
                    #add to the hybrid force:
                    #the parameters at indices 3 and 4 represent theta0 and k, respectively.
                    hybrid_force_parameters = [0.0, 0.0, 0.0,torsion_parameters[4], torsion_parameters[5], torsion_parameters[6]]
                    self._hybrid_system_forces['core_torsion_force'].addTorsion(hybrid_index_list[0], hybrid_index_list[1], hybrid_index_list[2], hybrid_index_list[3], hybrid_force_parameters)

                added_torsions.append(torsion_indices)

            #otherwise, just add the parameters to the regular force:
            else:
                self._hybrid_system_forces['standard_torsion_force'].addTorsion(hybrid_index_list[0], hybrid_index_list[1],
                                                                            hybrid_index_list[2], hybrid_index_list[3], torsion_parameters[4],
                                                                            torsion_parameters[5], torsion_parameters[6])

        for torsion_index in range(new_system_torsion_force.getNumTorsions()):
            torsion_parameters = new_system_torsion_force.getTorsionParameters(torsion_index)

            #get the indices in the hybrid system:
            hybrid_index_list = [self._new_to_hybrid_map[new_index] for new_index in torsion_parameters[:4]]
            hybrid_index_set = set(hybrid_index_list)

            #if any are part of the unique new atoms, we will add them to the standard torsion force:
            if len(hybrid_index_set.intersection(self._atom_classes['unique_new_atoms'])) > 0:
                self._hybrid_system_forces['standard_torsion_force'].addTorsion(hybrid_index_list[0], hybrid_index_list[1],
                                                                            hybrid_index_list[2], hybrid_index_list[3], torsion_parameters[4],
                                                                            torsion_parameters[5], torsion_parameters[6])

    def handle_nonbonded(self):
        """

        """
        old_system_nonbonded_force = self._old_system_forces['NonbondedForce']
        new_system_nonbonded_force = self._new_system_forces['NonbondedForce']
        hybrid_to_old_map = self._hybrid_to_old_map
        hybrid_to_new_map = self._hybrid_to_new_map

        # Define new global parameters for NonbondedForce
        self._hybrid_system_forces['standard_nonbonded_force'].addGlobalParameter('lambda_electrostatics_core', 0.0)
        self._hybrid_system_forces['standard_nonbonded_force'].addGlobalParameter('lambda_sterics_core', 0.0)
        self._hybrid_system_forces['standard_nonbonded_force'].addGlobalParameter("lambda_electrostatics_delete", 0.0)
        self._hybrid_system_forces['standard_nonbonded_force'].addGlobalParameter("lambda_electrostatics_insert", 0.0)

        #We have to loop through the particles in the system, because nonbonded force does not accept index
        for particle_index in range(self._hybrid_system.getNumParticles()):

            if particle_index in self._atom_classes['unique_old_atoms']:
                #get the parameters in the old system
                old_index = hybrid_to_old_map[particle_index]
                [charge, sigma, epsilon] = old_system_nonbonded_force.getParticleParameters(old_index)

                #add the particle to the hybrid custom sterics and electrostatics.
                check_index = self._hybrid_system_forces['core_sterics_force'].addParticle([sigma, epsilon, sigma, 0.0*epsilon, 1, 0])
                assert (particle_index == check_index ), "Attempting to add incorrect particle to hybrid system"

                # Add particle to the regular nonbonded force, but Lennard-Jones will be handled by CustomNonbondedForce
                check_index = self._hybrid_system_forces['standard_nonbonded_force'].addParticle(charge, sigma, 0.0*epsilon)
                assert (particle_index == check_index ), "Attempting to add incorrect particle to hybrid system"

                # Charge will be turned off at lambda_electrostatics_delete = 0, on at lambda_electrostatics_delete = 1
                self._hybrid_system_forces['standard_nonbonded_force'].addParticleParameterOffset('lambda_electrostatics_delete', particle_index, -charge, 0*sigma, 0*epsilon)

            elif particle_index in self._atom_classes['unique_new_atoms']:
                #get the parameters in the new system
                new_index = hybrid_to_new_map[particle_index]
                [charge, sigma, epsilon] = new_system_nonbonded_force.getParticleParameters(new_index)

                #add the particle to the hybrid custom sterics and electrostatics
                check_index = self._hybrid_system_forces['core_sterics_force'].addParticle([sigma, 0.0*epsilon, sigma, epsilon, 0, 1])
                assert (particle_index == check_index ), "Attempting to add incorrect particle to hybrid system"

                # Add particle to the regular nonbonded force, but Lennard-Jones will be handled by CustomNonbondedForce
                check_index = self._hybrid_system_forces['standard_nonbonded_force'].addParticle(0.0, sigma, 0.0)
                assert (particle_index == check_index ), "Attempting to add incorrect particle to hybrid system"

                # Charge will be turned off at lambda_electrostatics_insert = 0, on at lambda_electrostatics_insert = 1
                self._hybrid_system_forces['standard_nonbonded_force'].addParticleParameterOffset('lambda_electrostatics_insert', particle_index, +charge, 0, 0)

            elif particle_index in self._atom_classes['core_atoms']:
                #get the parameters in the new and old systems:
                old_index = hybrid_to_old_map[particle_index]
                [charge_old, sigma_old, epsilon_old] = old_system_nonbonded_force.getParticleParameters(old_index)
                new_index = hybrid_to_new_map[particle_index]
                [charge_new, sigma_new, epsilon_new] = new_system_nonbonded_force.getParticleParameters(new_index)

                #add the particle to the custom forces, interpolating between the two parameters
                check_index = self._hybrid_system_forces['core_sterics_force'].addParticle([sigma_old, epsilon_old, sigma_new, epsilon_new, 0, 0])
                assert (particle_index == check_index ), "Attempting to add incorrect particle to hybrid system"

                #still add the particle to the regular nonbonded force, but with zeroed out parameters.
                check_index = self._hybrid_system_forces['standard_nonbonded_force'].addParticle(charge_old, 0.5*(sigma_old+sigma_new), 0.0)
                assert (particle_index == check_index ), "Attempting to add incorrect particle to hybrid system"

                # Charge is charge_old at lambda_electrostatics = 0, charge_new at lambda_electrostatics = 1
                # TODO: We could also interpolate the Lennard-Jones here instead of core_sterics force so that core_sterics_force could just be softcore
                self._hybrid_system_forces['standard_nonbonded_force'].addParticleParameterOffset('lambda_electrostatics_core', particle_index, (charge_new - charge_old), 0, 0)

            #otherwise, the particle is in the environment
            else:
                #the parameters will be the same in new and old system, so just take the old parameters
                old_index = hybrid_to_old_map[particle_index]
                [charge, sigma, epsilon] = old_system_nonbonded_force.getParticleParameters(old_index)

                #add the particle to the hybrid custom sterics and electrostatics, but they dont change
                self._hybrid_system_forces['core_sterics_force'].addParticle([sigma, epsilon, sigma, epsilon, 0, 0])

                #add the environment atoms to the regular nonbonded force as well:
                self._hybrid_system_forces['standard_nonbonded_force'].addParticle(charge, sigma, epsilon)

        self._handle_interaction_groups()
        self._handle_hybrid_exceptions()
        self._handle_original_exceptions()

    def _generate_dict_from_exceptions(self, force):
        """
        This is a utility function to generate a dictionary of the form
        (particle1_idx, particle2_idx) : [exception parameters]. This will facilitate access and search of exceptions

        Parameters
        ----------
        force : openmm.NonbondedForce object
            a force containing exceptions

        Returns
        -------
        exceptions_dict : dict
            Dictionary of exceptions
        """
        exceptions_dict = {}

        for exception_index in range(force.getNumExceptions()):
            [index1, index2, chargeProd, sigma, epsilon] = force.getExceptionParameters(exception_index)
            exceptions_dict[(index1, index2)] = [chargeProd, sigma, epsilon]

        return exceptions_dict

    def _handle_interaction_groups(self):
        """
        Create the appropriate interaction groups for the custom nonbonded forces. The groups are:

        1) Unique-old - core
        2) Unique-old - environment
        3) Unique-new - core
        4) Unique-new - environment
        5) Core - environment
        6) Core - core

        Unique-old and Unique new are prevented from interacting this way, and intra-unique interactions occur in an
        unmodified nonbonded force.

        Must be called after particles are added to the Nonbonded forces
        """
        #get the force objects for convenience:
        sterics_custom_force = self._hybrid_system_forces['core_sterics_force']

        #also prepare the atom classes
        core_atoms = self._atom_classes['core_atoms']
        unique_old_atoms = self._atom_classes['unique_old_atoms']
        unique_new_atoms = self._atom_classes['unique_new_atoms']
        environment_atoms = self._atom_classes['environment_atoms']


        sterics_custom_force.addInteractionGroup(unique_old_atoms, core_atoms)

        sterics_custom_force.addInteractionGroup(unique_old_atoms, environment_atoms)

        sterics_custom_force.addInteractionGroup(unique_new_atoms, core_atoms)

        sterics_custom_force.addInteractionGroup(unique_new_atoms, environment_atoms)

        sterics_custom_force.addInteractionGroup(core_atoms, environment_atoms)

        sterics_custom_force.addInteractionGroup(core_atoms, core_atoms)

    def _handle_hybrid_exceptions(self):
        """
        Instead of excluding interactions that shouldn't occur, we provide exceptions for interactions that were zeroed
        out but should occur.
        Returns
        -------
        """
        old_system_nonbonded_force = self._old_system_forces['NonbondedForce']
        new_system_nonbonded_force = self._new_system_forces['NonbondedForce']

        import itertools
        #prepare the atom classes
        unique_old_atoms = self._atom_classes['unique_old_atoms']
        unique_new_atoms = self._atom_classes['unique_new_atoms']

        #get the list of interaction pairs for which we need to set exceptions:
        unique_old_pairs = list(itertools.combinations(unique_old_atoms, 2))
        unique_new_pairs = list(itertools.combinations(unique_new_atoms, 2))

        #add back the interactions of the old unique atoms, unless there are exceptions
        for atom_pair in unique_old_pairs:
            #since the pairs are indexed in the dictionary by the old system indices, we need to convert
            old_index_atom_pair = (self._hybrid_to_old_map[atom_pair[0]], self._hybrid_to_old_map[atom_pair[1]])

            #now we check if the pair is in the exception dictionary
            if old_index_atom_pair in self._old_system_exceptions:
                [chargeProd, sigma, epsilon] = self._old_system_exceptions[old_index_atom_pair]
                self._hybrid_system_forces['standard_nonbonded_force'].addException(atom_pair[0], atom_pair[1], chargeProd, sigma, epsilon)
                self._hybrid_system_forces['core_sterics_force'].addExclusion(atom_pair[0], atom_pair[1]) # add exclusion to ensure exceptions are consistent

            #check if the pair is in the reverse order and use that if so
            elif old_index_atom_pair[::-1] in self._old_system_exceptions:
                [chargeProd, sigma, epsilon] = self._old_system_exceptions[old_index_atom_pair[::-1]]
                self._hybrid_system_forces['standard_nonbonded_force'].addException(atom_pair[0], atom_pair[1], chargeProd, sigma, epsilon)
                self._hybrid_system_forces['core_sterics_force'].addExclusion(atom_pair[0], atom_pair[1]) # add exclusion to ensure exceptions are consistent

            #If it's not handled by an exception in the original system, we just add the regular parameters as an exception
            else:
                [charge0, sigma0, epsilon0] = self._old_system_forces['NonbondedForce'].getParticleParameters(old_index_atom_pair[0])
                [charge1, sigma1, epsilon1] = self._old_system_forces['NonbondedForce'].getParticleParameters(old_index_atom_pair[1])
                chargeProd = charge0*charge1
                epsilon = unit.sqrt(epsilon0*epsilon1)
                sigma = 0.5*(sigma0+sigma1)
                self._hybrid_system_forces['standard_nonbonded_force'].addException(atom_pair[0], atom_pair[1], chargeProd, sigma, epsilon)
                self._hybrid_system_forces['core_sterics_force'].addExclusion(atom_pair[0], atom_pair[1]) # add exclusion to ensure exceptions are consistent

        #add back the interactions of the new unique atoms, unless there are exceptions
        for atom_pair in unique_new_pairs:
            #since the pairs are indexed in the dictionary by the new system indices, we need to convert
            new_index_atom_pair = (self._hybrid_to_new_map[atom_pair[0]], self._hybrid_to_new_map[atom_pair[1]])

            #now we check if the pair is in the exception dictionary
            if new_index_atom_pair in self._new_system_exceptions:
                [chargeProd, sigma, epsilon] = self._new_system_exceptions[new_index_atom_pair]
                self._hybrid_system_forces['standard_nonbonded_force'].addException(atom_pair[0], atom_pair[1], chargeProd, sigma, epsilon)
                self._hybrid_system_forces['core_sterics_force'].addExclusion(atom_pair[0], atom_pair[1]) # add exclusion to ensure exceptions are consistent

            #check if the pair is present in the reverse order and use that if so
            elif new_index_atom_pair[::-1] in self._new_system_exceptions:
                [chargeProd, sigma, epsilon] = self._new_system_exceptions[new_index_atom_pair[::-1]]
                self._hybrid_system_forces['standard_nonbonded_force'].addException(atom_pair[0], atom_pair[1], chargeProd, sigma, epsilon)
                self._hybrid_system_forces['core_sterics_force'].addExclusion(atom_pair[0], atom_pair[1]) # add exclusion to ensure exceptions are consistent

            #If it's not handled by an exception in the original system, we just add the regular parameters as an exception
            else:
                [charge0, sigma0, epsilon0] = self._new_system_forces['NonbondedForce'].getParticleParameters(new_index_atom_pair[0])
                [charge1, sigma1, epsilon1] = self._new_system_forces['NonbondedForce'].getParticleParameters(new_index_atom_pair[1])
                chargeProd = charge0*charge1
                epsilon = unit.sqrt(epsilon0*epsilon1)
                sigma = 0.5*(sigma0+sigma1)
                self._hybrid_system_forces['standard_nonbonded_force'].addException(atom_pair[0], atom_pair[1], chargeProd, sigma, epsilon)
                self._hybrid_system_forces['core_sterics_force'].addExclusion(atom_pair[0], atom_pair[1]) # add exclusion to ensure exceptions are consistent

    def _handle_original_exceptions(self):
        """
        This method ensures that exceptions present in the original systems are present in the hybrid appropriately.
        """
        #get what we need to find the exceptions from the new and old systems:
        old_system_nonbonded_force = self._old_system_forces['NonbondedForce']
        new_system_nonbonded_force = self._new_system_forces['NonbondedForce']
        hybrid_to_old_map = {value: key for key, value in self._old_to_hybrid_map.items()}
        hybrid_to_new_map = {value: key for key, value in self._new_to_hybrid_map.items()}

        #first, loop through the old system's exceptions and add them to the hybrid appropriately:
        for exception_pair, exception_parameters in self._old_system_exceptions.items():

            [index1_old, index2_old] = exception_pair

            [chargeProd_old, sigma_old, epsilon_old] = exception_parameters

            #get hybrid indices:
            index1_hybrid = self._old_to_hybrid_map[index1_old]
            index2_hybrid = self._old_to_hybrid_map[index2_old]
            index_set = {index1_hybrid, index2_hybrid}


            #in this case, the interaction is only covered by the regular nonbonded force, and as such will be copied to that force
            #in the unique-old case, it is handled elsewhere due to internal peculiarities regarding exceptions
            if index_set.issubset(self._atom_classes['environment_atoms']):
                self._hybrid_system_forces['standard_nonbonded_force'].addException(index1_hybrid, index2_hybrid, chargeProd_old, sigma_old, epsilon_old)
                self._hybrid_system_forces['core_sterics_force'].addExclusion(index1_hybrid, index2_hybrid)

            #we have already handled unique old - unique old exceptions
            elif len(index_set.intersection(self._atom_classes['unique_old_atoms'])) == 2:
                continue

            #otherwise, check if one of the atoms in the set is in the unique_old_group and the other is not:
            elif len(index_set.intersection(self._atom_classes['unique_old_atoms'])) == 1:
                self._hybrid_system_forces['standard_nonbonded_force'].addException(index1_hybrid, index2_hybrid, chargeProd_old, sigma_old, epsilon_old)
                self._hybrid_system_forces['core_sterics_force'].addExclusion(index1_hybrid, index2_hybrid)

            #If the exception particles are neither solely old unique, solely environment, nor contain any unique old atoms, they are either core/environment or core/core
            #In this case, we need to get the parameters from the exception in the other (new) system, and interpolate between the two
            else:
                #first get the new indices.
                index1_new = hybrid_to_new_map[index1_hybrid]
                index2_new = hybrid_to_new_map[index2_hybrid]

                #get the exception parameters:
                new_exception_parms= self._find_exception(new_system_nonbonded_force, index1_new, index2_new)

                #if there's no new exception, then we should just set the exception parameters to be the nonbonded parameters
                if not new_exception_parms:
                    [charge1_new, sigma1_new, epsilon1_new] = new_system_nonbonded_force.getParticleParameters(index1_new)
                    [charge2_new, sigma2_new, epsilon2_new] = new_system_nonbonded_force.getParticleParameters(index2_new)

                    chargeProd_new = charge1_new * charge2_new
                    sigma_new = 0.5 * (sigma1_new + sigma2_new)
                    epsilon_new = unit.sqrt(epsilon1_new*epsilon2_new)
                else:
                    [index1_new, index2_new, chargeProd_new, sigma_new, epsilon_new] = new_exception_parms

                #interpolate between old and new
                exception_index = self._hybrid_system_forces['standard_nonbonded_force'].addException(index1_hybrid, index2_hybrid, chargeProd_old, sigma_old, epsilon_old)
                self._hybrid_system_forces['standard_nonbonded_force'].addExceptionParameterOffset('lambda_electrostatics_core', exception_index, (chargeProd_new - chargeProd_old), 0, 0)
                self._hybrid_system_forces['standard_nonbonded_force'].addExceptionParameterOffset('lambda_sterics_core', exception_index, 0, (sigma_new - sigma_old), (epsilon_new - epsilon_old))
                self._hybrid_system_forces['core_sterics_force'].addExclusion(index1_hybrid, index2_hybrid)

        #now, loop through the new system to collect remaining interactions. The only that remain here are
        #uniquenew-uniquenew, uniquenew-core, and uniquenew-environment. There might also be core-core, since not all
        #core-core exceptions exist in both
        for exception_pair, exception_parameters in self._new_system_exceptions.items():
            [index1_new, index2_new] = exception_pair
            [chargeProd_new, sigma_new, epsilon_new] = exception_parameters

            #get hybrid indices:
            index1_hybrid = self._new_to_hybrid_map[index1_new]
            index2_hybrid = self._new_to_hybrid_map[index2_new]

            index_set = {index1_hybrid, index2_hybrid}

            #if it's a subset of unique_new_atoms, then this is an intra-unique interaction and should have its exceptions
            #specified in the regular nonbonded force. However, this is handled elsewhere as above due to pecularities with exception handling
            if index_set.issubset(self._atom_classes['unique_new_atoms']):
                continue

            #look for the final class- interactions between uniquenew-core and uniquenew-environment. They are treated
            #similarly: they are simply on and constant the entire time (as a valence term)
            elif len(index_set.intersection(self._atom_classes['unique_new_atoms'])) > 0:
                self._hybrid_system_forces['standard_nonbonded_force'].addException(index1_hybrid, index2_hybrid, chargeProd_new, sigma_new, epsilon_new)
                self._hybrid_system_forces['core_sterics_force'].addExclusion(index1_hybrid, index2_hybrid)

            #however, there may be a core exception that exists in one system but not the other (ring closure)
            elif index_set.issubset(self._atom_classes['core_atoms']):

                # get the old indices
                try:
                    index1_old = self._topology_proposal.new_to_old_atom_map[index1_new]
                    index2_old = self._topology_proposal.new_to_old_atom_map[index2_new]
                except KeyError:
                    continue

                #see if it's also in the old nonbonded force. if it is, then we don't need to add it.
                #but if it's not, we need to interpolate
                if not self._find_exception(old_system_nonbonded_force, index1_old, index2_old):

                    [charge1_old, sigma1_old, epsilon1_old] = old_system_nonbonded_force.getParticleParameters(index1_old)
                    [charge2_old, sigma2_old, epsilon2_old] = old_system_nonbonded_force.getParticleParameters(index2_old)

                    chargeProd_old = charge1_old*charge2_old
                    sigma_old = 0.5 * (sigma1_old + sigma2_old)
                    epsilon_old = unit.sqrt(epsilon1_old*epsilon2_old)

                    exception_index = self._hybrid_system_forces['standard_nonbonded_force'].addException(index1_hybrid,
                                                                                                          index2_hybrid,
                                                                                                          chargeProd_old,
                                                                                                          sigma_old,
                                                                                                          epsilon_old)

                    self._hybrid_system_forces['standard_nonbonded_force'].addExceptionParameterOffset(
                        'lambda_electrostatics_core', exception_index, (chargeProd_new - chargeProd_old), 0, 0)

                    self._hybrid_system_forces['standard_nonbonded_force'].addExceptionParameterOffset('lambda_sterics_core',
                                                                                                       exception_index,
                                                                                                       0, (sigma_new - sigma_old),
                                                                                                       (epsilon_new - epsilon_old))

                    self._hybrid_system_forces['core_sterics_force'].addExclusion(index1_hybrid, index2_hybrid)


    def _find_exception(self, force, index1, index2):
        """
        Find the exception that corresponds to the given indices in the given system

        Parameters
        ----------
        force : openmm.NonbondedForce object
            System containing the exceptions
        index1 : int
            The index of the first atom (order is unimportant)
        index2 : int
            The index of the second atom (order is unimportant)

        Returns
        -------
        exception_parameters : list
            List of exception parameters
        """
        index_set = {index1, index2}

        #loop through the exceptions and try to find one matching the criteria
        for exception_idx in range(force.getNumExceptions()):
            exception_parameters = force.getExceptionParameters(exception_idx)
            if index_set==set(exception_parameters[:2]):
                return exception_parameters
        return []

    def _compute_hybrid_positions(self):
        """
        The positions of the hybrid system. Dimensionality is (n_environment + n_core + n_old_unique + n_new_unique)
        The positions are assigned by first copying all the mapped positions from the old system in, then copying the
        mapped positions from the new system. This means that there is an assumption that the positions common to old
        and new are the same (which is the case for perses as-is).

        Returns
        -------
        hybrid_positions : np.ndarray [n, 3]
            Positions of the hybrid system, in nm
        """
        #get unitless positions
        old_positions_without_units = np.array(self._old_positions.value_in_unit(unit.nanometer))
        new_positions_without_units = np.array(self._new_positions.value_in_unit(unit.nanometer))

        #determine the number of particles in the system
        n_atoms_hybrid = self._hybrid_system.getNumParticles()

        #initialize an array for hybrid positions
        hybrid_positions_array = np.zeros([n_atoms_hybrid, 3])

        #loop through the old system indices, and assign positions.
        for old_index, hybrid_index in self._old_to_hybrid_map.items():
            hybrid_positions_array[hybrid_index, :] = old_positions_without_units[old_index, :]

        #Do the same for new indices. Note that this overwrites some coordinates, but as stated above, the assumption
        #is that these are the same.
        for new_index, hybrid_index in self._new_to_hybrid_map.items():
            hybrid_positions_array[hybrid_index, :] = new_positions_without_units[new_index, :]

        return unit.Quantity(hybrid_positions_array, unit=unit.nanometers)

    def _create_topology(self):
        """
        Create an mdtraj topology corresponding to the hybrid system.
        This is purely for writing out trajectories--it is not expected to be parameterized.

        Returns
        -------
        hybrid_topology : mdtraj.Topology
        """
        #first, make an md.Topology of the old system:
        old_topology = md.Topology.from_openmm(self._topology_proposal.old_topology)

        #now make a copy for the hybrid:
        hybrid_topology = copy.deepcopy(old_topology)

        #next, make a topology of the new system:
        new_topology = md.Topology.from_openmm(self._topology_proposal.new_topology)

        added_atoms = dict()

        #get the core atoms in the new index system (as opposed to the hybrid index system). We will need this later
        core_atoms_new_indices = {self._hybrid_to_new_map[core_atom] for core_atom in self._atom_classes['core_atoms']}

        #now, add each unique new atom to the topology (this is the same order as the system)
        for particle_idx in self._topology_proposal.unique_new_atoms:
            new_system_atom = new_topology.atom(particle_idx)

            #first, we get the residue in the new system associated with this atom
            new_system_residue = new_system_atom.residue

            #next, we have to enumerate the other atoms in that residue to find mapped atoms
            new_system_atom_set = {atom.index for atom in new_system_residue.atoms}

            #Now, we find the subset of atoms that are mapped. These must be in the "core" category, since they are mapped
            #and part of a changing residue
            mapped_new_atom_indices = core_atoms_new_indices.intersection(new_system_atom_set)

            #Now get the old indices of the above atoms so that we can find the appropriate residue in the old system
            #for this we can use the new to old atom map
            mapped_old_atom_indices = [self._topology_proposal.new_to_old_atom_map[atom_idx] for atom_idx in mapped_new_atom_indices]

            #we can just take the first one--they all have the same residue
            first_mapped_old_atom_index = mapped_old_atom_indices[0]

            #get the atom object corresponding to this index from the hybrid (which is a deepcopy of the old)
            mapped_hybrid_system_atom = hybrid_topology.atom(first_mapped_old_atom_index)

            #get the residue that is relevant to this atom
            mapped_residue = mapped_hybrid_system_atom.residue

            #add the atom using the mapped residue
            added_atoms[particle_idx] = hybrid_topology.add_atom(new_system_atom.name, new_system_atom.element, mapped_residue)

        #now loop through the bonds in the new system, and if the bond contains a unique new atom, then add it to the hybrid topology
        for (atom1, atom2) in new_topology.bonds:
            atom1_index_in_hybrid = self._new_to_hybrid_map[atom1.index]
            atom2_index_in_hybrid = self._new_to_hybrid_map[atom2.index]

            #if at least one atom is in the unique new class, we need to add it to the hybrid system
            if atom1_index_in_hybrid in self._atom_classes['unique_new_atoms'] or atom2_index_in_hybrid in self._atom_classes['unique_new_atoms']:
                if atom1.index in self._atom_classes['unique_new_atoms']:
                    atom1_to_bond = added_atoms[atom1.index]
                else:
                    atom1_to_bond = atom1

                if atom2.index in self._atom_classes['unique_new_atoms']:
                    atom2_to_bond = added_atoms[atom2.index]
                else:
                    atom2_to_bond = atom2

                hybrid_topology.add_bond(atom1_to_bond, atom2_to_bond)

        return hybrid_topology

    def old_positions(self, hybrid_positions):
        """
        Get the positions corresponding to the old system

        Parameters
        ----------
        hybrid_positions : [n, 3] np.ndarray with unit
            The positions of the hybrid system

        Returns
        -------
        old_positions : [m, 3] np.ndarray with unit
            The positions of the old system
        """
        n_atoms_old = self._topology_proposal.n_atoms_old
        old_positions = unit.Quantity(np.zeros([n_atoms_old, 3]), unit=unit.nanometer)
        for idx in range(n_atoms_old):
            old_positions[idx, :] = hybrid_positions[idx, :]
        return old_positions

    def new_positions(self, hybrid_positions):
        """
        Get the positions corresponding to the new system.

        Parameters
        ----------
        hybrid_positions : [n, 3] np.ndarray with unit
            The positions of the hybrid system

        Returns
        -------
        new_positions : [m, 3] np.ndarray with unit
            The positions of the new system
        """
        n_atoms_new = self._topology_proposal.n_atoms_new
        new_positions = unit.Quantity(np.zeros([n_atoms_new, 3]), unit=unit.nanometer)
        for idx in range(n_atoms_new):
            new_positions[idx, :] = hybrid_positions[self._new_to_hybrid_map[idx], :]
        return new_positions

    @property
    def hybrid_system(self):
        """
        The hybrid system.

        Returns
        -------
        hybrid_system : openmm.System
            The system representing a hybrid between old and new topologies
        """
        return self._hybrid_system

    @property
    def new_to_hybrid_atom_map(self):
        """
        Give a dictionary that maps new system atoms to the hybrid system.

        Returns
        -------
        new_to_hybrid_atom_map : dict of {int, int}
            The mapping of atoms from the new system to the hybrid
        """
        return self._new_to_hybrid_map

    @property
    def old_to_hybrid_atom_map(self):
        """
        Give a dictionary that maps old system atoms to the hybrid system.

        Returns
        -------
        old_to_hybrid_atom_map : dict of {int, int}
            The mapping of atoms from the old system to the hybrid
        """
        return self._old_to_hybrid_map

    @property
    def hybrid_positions(self):
        """
        The positions of the hybrid system. Dimensionality is (n_environment + n_core + n_old_unique + n_new_unique)
        The positions are assigned by first copying all the mapped positions from the old system in, then copying the
        mapped positions from the new system.

        Returns
        -------
        hybrid_positions : [n, 3] Quantity nanometers
        """
        return self._hybrid_positions

    @property
    def hybrid_topology(self):
        """
        An MDTraj hybrid topology for the purpose of writing out trajectories. Note that we do not expect this to be
        able to be parameterized by the openmm forcefield class.

        Returns
        -------
        hybrid_topology : mdtraj.Topology
        """
        return self._hybrid_topology

    @property
    def omm_hybrid_topology(self):
        """
        An OpenMM format of the hybrid topology. Also cannot be used to parameterize system, only to write out trajectories.

        Returns
        -------
        hybrid_topology : simtk.openmm.app.Topology
        """
        return md.Topology.to_openmm(self._hybrid_topology)
